#!/bin/bash
set -e

pushd src || exit
rm -rf build
./build-lambda-layer.sh
popd || exit

pushd sample-apps2 || exit
rm -rf build*
./package-lambda-function.sh
popd || exit

pushd terraform2 || exit
terraform init
terraform apply -auto-approve
popd || exit