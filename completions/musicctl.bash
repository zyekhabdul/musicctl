# bash completion for musicctl

_musicctl_complete() {
  local cur prev cmd
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  cmd="${COMP_WORDS[1]:-}"

  local commands="tui play add search radio toggle pause next prev jump remove rm clear shuffle loop repeat mute speed vol volume seek status queue lyrics download dl local pl playlist sleep resume save cache history hist fav doctor stop version help"

  if ((COMP_CWORD == 1)); then
    COMPREPLY=( $(compgen -W "$commands --help --version" -- "$cur") )
    return 0
  fi

  case "$cmd" in
    fav)
      if ((COMP_CWORD == 2)); then
        COMPREPLY=( $(compgen -W "list ls add rm del play" -- "$cur") )
      fi
      ;;
    pl|playlist)
      if ((COMP_CWORD == 2)); then
        COMPREPLY=( $(compgen -W "ls list create add show play rm delete" -- "$cur") )
      elif ((COMP_CWORD == 3)) && [[ "$prev" != "ls" && "$prev" != "list" && "$prev" != "create" ]]; then
        local pdir="${MUSICCTL_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/musicctl}/playlists"
        if [[ -d "$pdir" ]]; then
          local names
          names="$(find "$pdir" -maxdepth 1 -type f -name '*.tsv' -printf '%f\n' 2>/dev/null | sed 's/\.tsv$//')"
          COMPREPLY=( $(compgen -W "$names" -- "$cur") )
        fi
      fi
      ;;
    loop|repeat)
      COMPREPLY=( $(compgen -W "off one all cycle" -- "$cur") )
      ;;
    sleep)
      COMPREPLY=( $(compgen -W "off status 15 30 45 60" -- "$cur") )
      ;;
    cache)
      COMPREPLY=( $(compgen -W "status clear" -- "$cur") )
      ;;
    lyrics)
      COMPREPLY=( $(compgen -W "--live" -- "$cur") )
      ;;
    status)
      COMPREPLY=( $(compgen -W "--json" -- "$cur") )
      ;;
    clear)
      COMPREPLY=( $(compgen -W "--all" -- "$cur") )
      ;;
    speed)
      COMPREPLY=( $(compgen -W "0.75 1.0 1.25 1.5 2.0 reset" -- "$cur") )
      ;;
    play|add)
      COMPREPLY=( $(compgen -f -- "$cur") )
      ;;
  esac
}

complete -F _musicctl_complete musicctl
