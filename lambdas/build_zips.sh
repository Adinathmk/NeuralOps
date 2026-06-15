#!/bin/bash
set -e

echo "Packaging lambda-kafka-to-sqs..."
cd lambda-kafka-to-sqs
zip -r ../lambda-kafka-to-sqs.zip handler.py
cd ..

echo "Packaging lambda-push-router..."
cd lambda-push-router
mkdir -p package
pip install -r requirements.txt -t package/
cp handler.py package/
cd package
zip -r ../../lambda-push-router.zip .
cd ..
rm -rf package
cd ..

echo "Packaging lambda-push-dispatch..."
cd lambda-push-dispatch
mkdir -p package
pip install -r requirements.txt -t package/
cp handler.py package/
cd package
zip -r ../../lambda-push-dispatch.zip .
cd ..
rm -rf package
cd ..

echo "All Lambdas packaged successfully into zip files!"
