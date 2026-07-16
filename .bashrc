# ~/.bashrc: executed by bash(1) for non-login shells.

# history file and size
export HISTFILE=~/.bash_history
export HISTSIZE=10000
export HISTFILESIZE=20000
export HISTCONTROL=ignoredups:erasedups
shopt -s histappend

# readline needs a real terminal type for arrow keys in docker exec -it
export TERM=xterm-256color

# skip the rest for non-interactive shells (scripts, pipes, etc.)
case $- in
    *i*) ;;
      *) return;;
esac

# up/down: search command history (prefix-aware when you have typed on the line)
bind '"\e[A": history-search-backward'
bind '"\e[B": history-search-forward'

# left/right: move the cursor within the current line
bind '"\e[C": forward-char'
bind '"\e[D": backward-char'
