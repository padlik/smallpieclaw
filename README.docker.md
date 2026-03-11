# Build and Run Instructions

## Development
#### Build the image
```
docker-compose build
```
#### Create data directory
```
mkdir -p data logs
```

#### Copy environment template
```
cp .env.example .env
```
Edit .env with your configuration

#### Run development version
```
docker-compose up -d
```

#### View logs
```
docker-compose logs -f tm-agent
```

## Production
#### Build with production config
```docker-compose -f docker-compose.prod.yml build```

#### Run production
```docker-compose -f docker-compose.prod.yml up -d```

#### Monitor
```docker-compose -f docker-compose.prod.yml logs -f```

## ARM64/Raspberry Pi
#### Build for ARM
```docker build -f Dockerfile.arm64 -t tm-agent:arm64 .```

#### Run
```
docker run -d \
  --name tm-agent \
  --restart unless-stopped \
  -e TM_TELEGRAM_TOKEN=your_token \
  -e TM_LLM_API_KEY=your_key \
  -v $(pwd)/data:/app/data \
  tm-agent:arm64
```
