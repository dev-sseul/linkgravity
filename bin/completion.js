const fs = require('fs');
const os = require('os');
const path = require('path');

// bash and fish read these directories lazily, on the first completion attempt, so neither needs
// an rc line. zsh's default fpath has no home-directory entry, so it gets three.
const TARGETS = {
    bash: { src: 'lgy.bash', dest: ['.local', 'share', 'bash-completion', 'completions', 'lgy'] },
    zsh: { src: '_lgy', dest: ['.local', 'share', 'zsh', 'site-functions', '_lgy'], rc: '.zshrc' },
    fish: { src: 'lgy.fish', dest: ['.config', 'fish', 'completions', 'lgy.fish'] },
};

const MARKER = '# linkgravity completion';

function zshRcBlock(dir) {
    return [
        '',
        MARKER,
        `fpath+=("${dir}")`,
        'autoload -Uz _lgy',
        'whence compdef > /dev/null && compdef _lgy lgy linkgravity',
        '',
    ].join('\n');
}

function installCompletion(shell = path.basename(process.env.SHELL || '')) {
    const target = TARGETS[shell];
    if (!target) return null;

    const dest = path.join(os.homedir(), ...target.dest);
    let rcUpdated = false;
    let cleaned = false;
    try {
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.copyFileSync(path.join(__dirname, 'completions', target.src), dest);

        if (target.rc) {
            const rc = path.join(os.homedir(), target.rc);
            let existing = fs.existsSync(rc) ? fs.readFileSync(rc, 'utf8') : '';
            // Strip any pre-marker `eval "$(lgy completion ...)"` line - lgy never had that subcommand.
            const stale = /^\s*eval "\$\(lgy completion[^)]*\)"\s*$/gm;
            if (stale.test(existing)) {
                fs.writeFileSync(rc, existing.replace(stale, '').replace(/\n{3,}/g, '\n\n'));
                existing = fs.readFileSync(rc, 'utf8');
                cleaned = true;
            }
            if (!existing.includes(MARKER)) {
                fs.appendFileSync(rc, zshRcBlock(path.dirname(dest)));
                rcUpdated = true;
            }
        }
    } catch {
        return null;
    }
    return {
        shell,
        file: dest,
        rc: rcUpdated ? path.join(os.homedir(), target.rc) : null,
        cleaned,
    };
}

module.exports = { installCompletion };
