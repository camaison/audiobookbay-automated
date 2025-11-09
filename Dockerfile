# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the app directory contents into the container at /app
COPY app/ /app/

# Create data directory for persistent storage
RUN mkdir -p /app/data

# Install any necessary dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# Expose the port the app runs on
EXPOSE 5078

# Define the command to run the application
CMD ["python", "app.py"]
