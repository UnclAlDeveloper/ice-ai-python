# ~/.profile: executed by the shell at login.

export TERM="${TERM:-xterm-256color}"

# dash and other /bin/sh implementations: start bash so readline and .bashrc work
if [ -n "${PS1:-}" ] && [ -z "${BASH_VERSION:-}" ] && [ -x /bin/bash ]; then
    exec /bin/bash --login
fi

if [ -n "${BASH_VERSION:-}" ] && [ -f "${HOME}/.bashrc" ]; then
    . "${HOME}/.bashrc"
fi
