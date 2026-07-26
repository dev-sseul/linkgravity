def parse_shell_commands(cmd: str) -> list[str]:
    """
    Parses a shell command string into individual sub-commands
    separated by &&, ||, ;, or \n, respecting single and double quotes.
    """
    sub_cmds = []
    current = []
    in_single = False
    in_double = False

    i = 0
    while i < len(cmd):
        c = cmd[i]

        if c == "'" and not in_double:
            in_single = not in_single
            current.append(c)
        elif c == '"' and not in_single:
            if i > 0 and cmd[i - 1] == "\\":
                current.append(c)
            else:
                in_double = not in_double
                current.append(c)
        elif not in_single and not in_double:
            if c == "\\":
                current.append(c)
                if i + 1 < len(cmd):
                    current.append(cmd[i + 1])
                    i += 1
            elif c == "\n":
                if current:
                    sub_cmds.append("".join(current))
                    current = []
            elif c == ";":
                if current:
                    sub_cmds.append("".join(current))
                    current = []
            elif c == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
                if current:
                    sub_cmds.append("".join(current))
                    current = []
                i += 1
            elif c == "|":
                if i + 1 < len(cmd) and cmd[i + 1] == "|":
                    if current:
                        sub_cmds.append("".join(current))
                        current = []
                    i += 1
                else:
                    if current:
                        sub_cmds.append("".join(current))
                        current = []
            else:
                current.append(c)
        else:
            current.append(c)
        i += 1

    if current:
        sub_cmds.append("".join(current))

    return [c.strip() for c in sub_cmds if c.strip()]
