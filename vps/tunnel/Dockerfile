FROM alpine:3.24.3

# Compte sans shell ni mot de passe. "*" au lieu de "!" : sshd refuse un compte verrouillé,
# même par clé.
RUN apk add --no-cache openssh-server \
    && adduser -D -H -h /var/empty -s /sbin/nologin tunnel \
    && sed -i 's/^tunnel:!/tunnel:*/' /etc/shadow

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh

EXPOSE 2222
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
