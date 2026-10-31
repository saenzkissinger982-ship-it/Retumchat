# RetumChat Server

Servidor de chat de *El Retumbero 505*.

Corre con Python 3 y guarda los mensajes en SQLite. Habla el mismo protocolo que la app RetumChat (texto, imagenes, audios, salas y privados cifrados).

## Endpoints

- `POST /{canal}` : envia un mensaje de texto, o un archivo si mandas el header `Filename`
- `GET /{canal}/json?poll=1&since=all` : historial
- `GET /{canal}/sse?since=N` : conexion en vivo
- `GET /file/{archivo}` : descarga de imagenes y audios
- `GET /health` : estado del servidor

## Docker

```
docker build -t retumchat .
docker run -p 8090:8090 retumchat
```

## Deploy en Render

Web Service con runtime Docker, plan Free, health check path `/health`.
