#!/bin/bash
# Copyright (c) 2018, NVIDIA CORPORATION.
##############################################
# Dask cuML GPU build and test script for CI #
##############################################
set -e

# Logger function for build status output
function logger() {
  echo -e "\n>>>> $@\n"
}

# Set path and build parallel level
export PATH=/conda/bin:/usr/local/cuda/bin:$PATH
export PARALLEL_LEVEL=4
export CUDA_REL=${CUDA_VERSION%.*}

# Set versions of packages needed to be grabbed
export CUDF_VERSION=0.7.*
export NVSTRINGS_VERSION=0.7.*
export RMM_VERSION=0.7.*
export CUML_VERSION=0.7.*

# Set home to the job's workspace
export HOME=$WORKSPACE

################################################################################
# SETUP - Check environment
################################################################################

logger "Check environment..."
env

logger "Check GPU usage..."
nvidia-smi

logger "Activate conda env..."
source activate gdf

logger "conda install -c nvidia/label/cuda$CUDA_REL -c rapidsai/label/cuda$CUDA_REL -c conda-forge -c defaults -c rapidsai-nightly/label/cuda$CUDA_REL cuml dask distributed cudf dask-cudf dask-cuda"
conda install -c nvidia/label/cuda$CUDA_REL -c rapidsai/label/cuda$CUDA_REL -c conda-forge -c defaults -c rapidsai-nightly/label/cuda$CUDA_REL cuml dask distributed cudf dask-cudf dask-cuda

logger "Check versions..."
python --version
$CC --version
$CXX --version
conda list

################################################################################
# BUILD - Build dask-cuml from source
################################################################################

logger "Build Dask cuML..."
cd $WORKSPACE/
python setup.py build_ext --inplace

################################################################################
# TEST - Run GoogleTest and py.tests for libcuml and cuML
################################################################################

logger "Python py.test for Dask cuML..."
cd $WORKSPACE
py.test --cache-clear --junitxml=${WORKSPACE}/junit-cuml.xml -v
