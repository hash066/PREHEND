# Stage 1: Build the Vite React application
FROM node:22-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Final FastAPI backend container
FROM python:3.10-slim
WORKDIR /app

COPY host/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend files
COPY host/ .

# Copy built frontend assets into the expected parent path /frontend/dist
COPY --from=frontend-builder /frontend/dist /frontend/dist

EXPOSE 8000

CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
