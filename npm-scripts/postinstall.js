const { execSync } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');
const { pip: venvPip, workspaceDir } = require('./venv-paths');

console.log('⚙️  Setting up Python Virtual Environment...');
console.log(`   (in ${path.join(workspaceDir, 'venv')} - not inside this install, so it survives`);
console.log('    package updates/reinstalls and works the same whether this is a global');
console.log('    `npm install -g linkgravity` or a local dev clone.)');

// System python (not the venv's) - only used to create the venv below; every other
// script goes through venv-paths.js instead.
const isWin = os.platform() === 'win32';
const pyCmd = isWin ? 'python' : 'python3';

async function main() {
    try {
        // Fixed workspace dir, not cwd - see venv-paths.js for why.
        fs.mkdirSync(workspaceDir, { recursive: true });
        execSync(`${pyCmd} -m venv "${path.join(workspaceDir, 'venv')}"`, { stdio: 'inherit' });

        console.log('📦 Installing Python dependencies...');
        execSync(`"${venvPip}" install -r requirements.txt`, { stdio: 'inherit' });

        console.log('🎙️  Installing Voice Service dependencies...');
        execSync('npm install', {
            stdio: 'inherit',
            cwd: path.join(__dirname, '..', 'voice-service'),
        });

        // Own try/catch: agy may not be installed yet on a brand new machine, and that shouldn't fail the rest of the install.
        try {
            require('./register-hook')({ allowFirstTimeCreate: false });
        } catch (err) {
            console.warn(
                `⚠️  Couldn't register the agy tool-approval hook: ${err.message.split('\n')[0]}`,
            );
        }

        console.log('✅ Installation complete!');
    } catch (error) {
        console.error('❌ Installation failed. Please ensure Python 3.10+ is installed.');
        process.exit(1);
    }
}

main();
