_lgy_completion() {
  local cur prev
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD - 1]}"

  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "version start stop restart logs status enable disable setup update help" -- "$cur"))
    return
  fi

  if [ "${COMP_WORDS[1]}" = "logs" ]; then
    case "$prev" in
    -n | --tail) return ;;
    esac
    COMPREPLY=($(compgen -W "-f -n --tail -t --timestamp" -- "$cur"))
  fi
}
complete -F _lgy_completion lgy linkgravity
