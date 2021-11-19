#!/bin/bash
# Copyright 2021 InstaDeep Ltd. All rights reserved.
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

# Bash settings: fail on any error and display all commands being run.
set -e
set -x

# Update
apt-get update

# Python must be 3.6 or higher.
python --version

# Install dependencies.
pip install --upgrade pip setuptools
pip --version

# Set up a virtual environment.
pip install virtualenv
virtualenv mava_testing
source mava_testing/bin/activate

# Fix module 'enum' has no attribute 'IntFlag' for py3.6
pip uninstall -y enum34

pip install .[testing_formatting]
# Check code follows black formatting.
black --check .
# stop the build if there are Python syntax errors or undefined names
flake8 .  --count --select=E9,F63,F7,F82 --show-source --statistics
# exit-zero treats all errors as warnings.
flake8 . --count --exit-zero --statistics

# Check types.
mypy --exclude '(docs|build)/$' .

# Check docstring code coverage.
interrogate -c pyproject.toml
# Clean-up.
deactivate
rm -rf mava_testing/