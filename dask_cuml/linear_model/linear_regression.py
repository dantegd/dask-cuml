#
# Copyright (c) 2019, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import cudf
import itertools
import dask_cudf
import numpy as np

from cuml import ols_spmg as cuOLS
from dask import delayed
from dask_cuml.core import new_ipc_thread, parse_host_port
from dask_cuml.core import device_of_devicendarray, build_host_dict
from dask.distributed import wait, default_client
from math import ceil
from toolz import first
from tornado import gen


class LinearRegression(object):
    """
    Model-Parallel Multi-GPU Linear Regression Model. Single Process Multi GPU
    supported currently
    """
    def __init__(self, fit_intercept=True, normalize=False):

        """
        Initializes the liner regression class.

        Parameters
        ----------
        fit_intercept: boolean. For more information, see `scikitlearn's OLS
        <https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LinearRegression.html>`_.
        normalize: boolean. For more information, see `scikitlearn's OLS
        <https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LinearRegression.html>`_.

        """
        self.coef_ = None
        self.intercept_ = None
        self.fit_intercept = fit_intercept
        self.normalize = normalize

    def _build_params_map(self):
        return {"fit_intercept": self.fit_intercept,
                "normalize": self.normalize}

    def fit(self, X, y):
        """
        Fits a multi-gpu linear regression model such that each the resulting
        coefficients are also distributed across the GPUs.
        :param futures:
        :return:
        """
        client = default_client()

        coef_df = cudf.Series(np.zeros(X.shape[1]))

        self.coef_ = dask_cudf.from_cudf(coef_df,
                                         npartitions=X.npartitions).persist()

        del(coef_df)

        intercept = client.sync(self._do_fit, X, y, self.coef_)

        print(self.coef_.compute())

        self.intercept_ = intercept

    @gen.coroutine
    def _do_fit(self, X_df, y_df, coef_df):

        print("Fitting " + str(X_df.shape[1]) + " columns on " +
              str(X_df.npartitions) + " partitions.")

        client = default_client()

        # Finding location of parts of y_df to distribute columns of X_df
        loc_dict = {}
        yield wait(y_df)
        tt = yield client.who_has(y_df)
        location = tuple(tt.values())
        for i in range(X_df.npartitions):
            part_number = eval(list(tt.keys())[i])[1]
            loc_dict[part_number] = location[i]

        # Lets divide the columns evenly, matching the order of the labels
        part_size = ceil(X_df.shape[1] / X_df.npartitions)

        # We scatter delayed operations to gather columns on the workers
        scattered = []
        for i in range(X_df.npartitions):
            up_limit = min((i+1)*part_size, X_df.shape[1])
            cols = X_df.columns.values[i*part_size:up_limit]
            loc_cudf = X_df[cols]
            yield wait(loc_cudf)
            scattered.append(client.submit(preprocess_on_worker,
                                           loc_cudf,
                                           workers=[parse_host_port(
                                                    str(loc_dict[i])[:-3])]))
            yield wait(scattered)
            del(loc_cudf)

        # Break apart Dask.array/dataframe into chunks/parts
        # data_parts = map(delayed, scattered)
        data_parts = scattered
        label_parts = y_df.to_delayed()
        coef_parts = self.coef_.to_delayed()
        if isinstance(data_parts, np.ndarray):
            assert data_parts.shape[1] == 1
            data_parts = data_parts.flatten().tolist()
        if isinstance(label_parts, np.ndarray):
            assert label_parts.ndim == 1 or label_parts.shape[1] == 1
            label_parts = label_parts.flatten().tolist()

        # Arrange parts into pairs.  This enforces co-locality
        parts = list(map(delayed, zip(data_parts, label_parts, coef_parts)))
        parts = client.compute(parts)  # Start computation in the background
        yield wait(parts)

        for part in parts:
            if part.status == 'error':
                yield part  # trigger error locally

        # A dict in the form of { part_key: part }
        key_to_part_dict = dict([(str(part.key), part) for part in parts])

        who_has = yield client.who_has(parts)

        worker_parts = {}
        for key, workers in who_has.items():
            worker = parse_host_port(first(workers))
            if worker not in worker_parts:
                worker_parts[worker] = []
            worker_parts[worker].append(key_to_part_dict[key])

        """
        Create IP Handles on each worker hosting input data
        """

        # Format of input_devarrays = ([(X, y)..], dev)
        input_devarrays = [(worker, client.submit(inputs_to_device_arrays,
                                                  part, workers=[worker]))
                           for worker, part in worker_parts.items()]

        yield wait(input_devarrays)

        """
        Gather IPC handles for each worker and call _fit() on each worker
        containing data.
        """
        exec_node = input_devarrays[0][0]

        # Need to fetch parts on worker
        on_worker = list(filter(lambda x: x[0] == exec_node, input_devarrays))
        not_on_worker = list(filter(lambda x: x[0] != exec_node,
                                    input_devarrays))

        ipc_handles = [client.submit(get_input_ipc_handles, future,
                                     workers=[a_worker])
                       for a_worker, future in not_on_worker]

        raw_arrays = [future for a_worker, future in on_worker]

        # IPC Handles are loaded in separate threads on worker so they can be
        # used to make calls through cython
        # Calls _fit_on_worker defined in the bottom
        intercept = client.submit(_fit_on_worker, (ipc_handles, raw_arrays),
                                  self._build_params_map(),
                                  workers=[exec_node])

        yield wait(intercept)

        return gen.Return((coef_df, intercept))

    def predict(self, X):
        """
        Predict values for the multi-gpu linear regression model by making
        calls to the predict function with dask-cudf objects.

        :param df:
            a dask-cudf with data distributed one worker per GPU
        :return:
            a dask-cudf containing outputs of the linear regression
        """

        client = default_client()
        pred_df = cudf.Series(np.zeros(len(X)))
        preds = dask_cudf.from_cudf(pred_df,
                                    npartitions=X.npartitions).persist()
        return client.sync(self._do_predict, X, preds, self.coef_,
                           self.intercept)

    @gen.coroutine
    def _do_predict(self, X_df, pred_df, coef_df, intercept):

        print("Predicting " + str(X_df.shape[1]) + " columns on " +
              str(X_df.npartitions) + " partitions.")

        client = default_client()

        # Finding location of parts of coef_df to distribute columns of X_df
        loc_dict = {}
        yield wait(coef_df)
        tt = yield client.who_has(coef_df)
        location = tuple(tt.values())
        for i in range(X_df.npartitions):
            part_number = eval(list(tt.keys())[i])[1]
            loc_dict[part_number] = location[i]

        # Lets divide the columns evenly, matching the order of the labels
        part_size = ceil(X_df.shape[1] / X_df.npartitions)

        # We scatter delayed operations to gather columns on the workers
        scattered = []
        for i in range(X_df.npartitions):
            up_limit = min((i+1)*part_size, X_df.shape[1])
            cols = X_df.columns.values[i*part_size:up_limit]
            loc_cudf = X_df[cols]
            yield wait(loc_cudf)
            scattered.append(client.submit(preprocess_on_worker,
                                           loc_cudf,
                                           workers=[parse_host_port(
                                                    str(loc_dict[i])[:-3])]))
            yield wait(scattered)
            del(loc_cudf)

        # Break apart Dask.array/dataframe into chunks/parts
        # data_parts = map(delayed, scattered)
        data_parts = scattered
        pred_parts = pred_df.to_delayed()
        coef_parts = coef_df.to_delayed()

        # Arrange parts into pairs.  This enforces co-locality
        parts = list(map(delayed, zip(data_parts, coef_parts, pred_parts)))
        parts = client.compute(parts)  # Start computation in the background
        yield wait(parts)

        for part in parts:
            if part.status == 'error':
                yield part  # trigger error locally

        # A dict in the form of { part_key: part }
        key_to_part_dict = dict([(str(part.key), part) for part in parts])

        who_has = yield client.who_has(parts)

        worker_parts = {}
        for key, workers in who_has.items():
            worker = parse_host_port(first(workers))
            if worker not in worker_parts:
                worker_parts[worker] = []
            worker_parts[worker].append(key_to_part_dict[key])

        """
        Create IP Handles on each worker hosting input data
        """

        # Format of input_devarrays = ([(X, y)..], dev)
        input_devarrays = [(worker, client.submit(inputs_to_device_arrays,
                                                  part, workers=[worker]))
                           for worker, part in worker_parts.items()]

        yield wait(input_devarrays)

        """
        Gather IPC handles for each worker and call _fit() on each worker
        containing data.
        """
        exec_node = input_devarrays[0][0]

        # Need to fetch parts on worker
        on_worker = list(filter(lambda x: x[0] == exec_node, input_devarrays))
        not_on_worker = list(filter(lambda x: x[0] != exec_node,
                                    input_devarrays))

        ipc_handles = [client.submit(get_input_ipc_handles, future,
                                     workers=[a_worker])
                       for a_worker, future in not_on_worker]

        raw_arrays = [future for a_worker, future in on_worker]

        # IPC Handles are loaded in separate threads on worker so they can be
        # used to make calls through cython
        # Calls _predict_on_worker defined in the bottom
        ret = client.submit(_predict_on_worker, (ipc_handles, raw_arrays),
                            self._build_params_map(), workers=[exec_node])

        yield wait(ret)
        return gen.Return(pred_df)

    def _build_host_dict(self, gpu_futures, client):

        who_has = client.who_has(gpu_futures)

        key_to_host_dict = {}
        for key in who_has:
            key_to_host_dict[key] = parse_host_port(who_has[key][0])

        hosts_to_key_dict = {}
        for key, host in key_to_host_dict.items():
            if host not in hosts_to_key_dict:
                hosts_to_key_dict[host] = set([key])
            else:
                hosts_to_key_dict[host].add(key)

        workers = [key[0] for key in list(who_has.values())]
        return build_host_dict(workers)


def _fit_on_worker(data, params):
    ipc_dev_list, devarrs_dev_list = data

    # TODO: One ipc thread per device instead of per x,y,coef tuple
    open_ipcs = []
    for p, dev in ipc_dev_list:
        for x, y, coef in p:
            ipct = new_ipc_thread([x, y, coef], dev)
            open_ipcs.append(ipct)

    alloc_info = list(itertools.chain([t.info() for t in open_ipcs]))
    alloc_info.extend(
        list(itertools.chain(
            [[(build_alloc_info(X)[0], build_alloc_info(y)[0],
               build_alloc_info(coef)[0]) for X, y, coef in p]
             for p, dev in devarrs_dev_list])))

    # Call _fit() w/ all the cudfs on this worker and our coefficient pointers
    # m = _fit(alloc_info, params)

    try:
        ols = cuOLS()

        intercept = ols.fit(alloc_info, params)

    except Exception as e:
        print("FAILURE in FIT: " + str(e))

    [t.close() for t in open_ipcs]
    [t.join() for t in open_ipcs]

    return intercept


def _predict_on_worker(data, intercept):
    ipc_dev_list, devarrs_dev_list = data

    # TODO: One ipc thread per device instead of per x,y,coef tuple
    open_ipcs = []
    for p, dev in ipc_dev_list:
        for x, coef, pred in p:
            ipct = new_ipc_thread([x, coef, pred], dev)
            open_ipcs.append(ipct)

    alloc_info = list(itertools.chain([t.info() for t in open_ipcs]))
    alloc_info.extend(
        list(itertools.chain(
            [[(build_alloc_info(X)[0], build_alloc_info(coef)[0],
               build_alloc_info(pred)[0]) for X, coef, pred in p]
             for p, dev in devarrs_dev_list])))

    try:
        ols = cuOLS()

        ols.fit(alloc_info, intercept)

    except Exception as e:
        print("FAILURE in FIT: " + str(e))

    [t.close() for t in open_ipcs]
    [t.join() for t in open_ipcs]


def group(lst, n):
    for i in range(0, len(lst), n):
        val = lst[i:i+n]
        if len(val) == n:
            yield tuple(val)


def build_alloc_info(p): return [p.__cuda_array_interface__]


def get_input_ipc_handles(arr):

    arrs, dev = arr
    ret = [(X.get_ipc_handle(),
            y.get_ipc_handle(),
            coef.get_ipc_handle()) for X, y, coef in arrs]

    return ret, dev


def as_gpu_matrix(arr):
    blap = arr.compute()
    mat = blap.as_gpu_matrix(order="F")
    dev = device_of_devicendarray(mat)

    # Return canonical device id as string
    return mat, dev


def to_gpu_array(arr):

    mat = arr.to_gpu_array()
    dev = device_of_devicendarray(mat)

    # Return canonical device id as string
    return mat, dev


def inputs_to_device_arrays(arr):
    """
    :param arr:
        A tuple in the form of (X, y, coef)
    :return:
    """

    mats = [(X.compute(order='F').as_gpu_matrix(),
             y.to_gpu_array(),
             coef.to_gpu_array()) for X, y, coef in arr]

    dev = device_of_devicendarray(mats[0][0])

    # Return canonical device id as string
    return mats, dev


def extract_part(data, part):
    return data[part]


def preprocess_on_worker(arr):
    return arr