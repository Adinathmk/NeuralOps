# Build the packager image
Write-Host "Building Docker image for packaging Lambdas..."
docker build -t lambda-packager -f Dockerfile.packager .

# Run the container to build zip files
Write-Host "Running container to package dependencies and zip..."
docker run --rm -v "${PWD}:/workspace" lambda-packager

Write-Host "Done! The zip files are now in your lambdas folder."
