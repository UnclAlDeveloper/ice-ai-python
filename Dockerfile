FROM python:3.12-slim

# install postgresql-client-18 from the official pgdg apt repo (must be >= server major; our DBs run 18.x)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg lsb-release \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-18 procps tmux \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# install real google chrome for stealth launches (channel=chrome) plus the
# bundled chromium build as a fallback when the chrome channel is unavailable
RUN playwright install --with-deps chrome chromium

COPY . .
COPY .bashrc /root/.bashrc
COPY .bash_profile /root/.bash_profile
COPY .profile /root/.profile
RUN ln -sf /bin/bash /bin/sh \
 && usermod -s /bin/bash root
RUN chmod +x entrypoint.sh

EXPOSE 8005

CMD ["./entrypoint.sh"]
