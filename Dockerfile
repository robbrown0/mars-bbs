FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS stockfish
ARG STOCKFISH_URL=https://github.com/official-stockfish/Stockfish/releases/download/sf_19/stockfish-linux-x86-64-universal.tar.gz
ARG STOCKFISH_SHA256=9defc0d4e55d49c65a6d042f3e571a39fcea499ade6dbe741b53b8c65e03611f
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/* \
    && curl --fail --location --proto '=https' --tlsv1.2 "$STOCKFISH_URL" -o /tmp/stockfish.tar.gz \
    && echo "$STOCKFISH_SHA256  /tmp/stockfish.tar.gz" | sha256sum --check --strict \
    && tar -xzf /tmp/stockfish.tar.gz -C /tmp \
    && install -D -m 0555 /tmp/stockfish/stockfish-linux-x86-64-universal /out/bin/stockfish \
    && install -D -m 0444 /tmp/stockfish/Copying.txt /out/doc/Copying.txt \
    && install -D -m 0444 /tmp/stockfish/AUTHORS /out/doc/AUTHORS \
    && cp -a /tmp/stockfish/src /out/source

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS service
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-sgp4 libsodium23 ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
COPY --from=stockfish /out/bin/stockfish /usr/local/bin/stockfish
COPY --from=stockfish /out/doc/ /usr/share/doc/stockfish/
COPY --from=stockfish /out/source/ /usr/src/stockfish/
RUN python3 -m venv --system-site-packages /opt/venv && /opt/venv/bin/pip install --no-cache-dir chess==1.11.2
ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONPATH=/opt/mars-v2 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MARS_STOCKFISH=/usr/local/bin/stockfish
WORKDIR /opt/mars-v2
COPY marsv2/ marsv2/
COPY assets/ assets/
RUN find /opt/mars-v2 -type d -exec chmod 0555 {} + && find /opt/mars-v2 -type f -exec chmod 0444 {} +
RUN install -d -o 21001 -g 21001 -m 0755 /run/mars-v2/auth && install -d -o 21001 -g 21001 -m 0700 /var/lib/mars-v2/auth && install -d -o 21002 -g 21002 -m 0755 /run/mars-v2/session && install -d -o 21002 -g 21002 -m 0700 /var/lib/mars-v2/session && install -d -o 21003 -g 21003 -m 0755 /run/mars-v2/mail && install -d -o 21003 -g 21003 -m 0700 /var/lib/mars-v2/mail && install -d -o 21004 -g 21004 -m 0755 /run/mars-v2/board && install -d -o 21004 -g 21004 -m 0700 /var/lib/mars-v2/board && install -d -o 21005 -g 21005 -m 0755 /run/mars-v2/file && install -d -o 21005 -g 21005 -m 0700 /var/lib/mars-v2/file && install -d -o 21006 -g 21006 -m 0755 /run/mars-v2/chat && install -d -o 21006 -g 21006 -m 0700 /var/lib/mars-v2/chat && install -d -o 21007 -g 21007 -m 0755 /run/mars-v2/irc && install -d -o 21007 -g 21007 -m 0700 /var/lib/mars-v2/irc && install -d -o 21008 -g 21008 -m 0755 /run/mars-v2/external-irc && install -d -o 21008 -g 21008 -m 0700 /var/lib/mars-v2/external-irc && install -d -o 21009 -g 21009 -m 0755 /run/mars-v2/information && install -d -o 21009 -g 21009 -m 0700 /var/lib/mars-v2/information && install -d -o 21010 -g 21010 -m 0755 /run/mars-v2/game && install -d -o 21010 -g 21010 -m 0700 /var/lib/mars-v2/game && install -d -o 21011 -g 21011 -m 0755 /run/mars-v2/door && install -d -o 21011 -g 21011 -m 0700 /var/lib/mars-v2/door && true
USER 21001:21001

FROM service AS gateway
USER root
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server tini && find /etc/ssh -type f -name 'ssh_host_*' -delete && rm -rf /var/lib/apt/lists/* && groupadd --gid 29999 bbs && useradd --uid 29999 --gid 29999 --home-dir /run/mars-v2-sshd --shell /opt/mars-v2/bin/bbs-login bbs && usermod --password '' bbs && install -d -m 0700 /var/lib/mars-v2-ssh && install -d -m 0755 /var/empty /run/mars-v2 && find / -xdev -type f -perm /6000 -exec chmod a-s {} +
COPY bin/ /opt/mars-v2/bin/
COPY container/gateway-entrypoint.sh /usr/local/bin/gateway-entrypoint.sh
COPY ssh/sshd_config /etc/ssh/sshd_config_v2
RUN chmod 0555 /opt/mars-v2/bin/bbs-login /usr/local/bin/gateway-entrypoint.sh
EXPOSE 2222
ENTRYPOINT ["/usr/bin/tini","-g","--","/usr/local/bin/gateway-entrypoint.sh"]

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS irc
RUN apt-get update && apt-get install -y --no-install-recommends ngircd openssl tini ca-certificates && rm -rf /var/lib/apt/lists/* && groupadd --gid 10002 marsirc && useradd --uid 10002 --gid 10002 --no-create-home marsirc
COPY irc/ /opt/mars-irc/
COPY container/irc-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN find /opt/mars-irc -type d -exec chmod 0555 {} + && find /opt/mars-irc -type f -exec chmod 0444 {} + && mkdir -p /run/mars-irc /etc/ngircd/conf.d && chown 10002:10002 /run/mars-irc && chmod 0555 /usr/local/bin/entrypoint.sh
USER 10002:10002
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

FROM service AS test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/* && /opt/venv/bin/pip install --no-cache-dir pexpect==4.9.0 ptyprocess==0.7.0
COPY tests/ /opt/mars-v2/tests/
RUN find /opt/mars-v2/tests -type d -exec chmod 0555 {} + && find /opt/mars-v2/tests -type f -exec chmod 0444 {} +
CMD ["python3","-m","unittest","discover","-s","tests","-v"]
