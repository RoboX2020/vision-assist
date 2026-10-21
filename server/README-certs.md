# Local TLS certs

The self-signed dev certificates (`key.pem`, `server.key`, `cert.pem`, `server.crt`)
were removed from the repo. Generate them locally:

    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout server.key -out server.crt -days 365 \
      -subj "/CN=localhost"

Never commit private keys or certs.
