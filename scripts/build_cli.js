const { spawnSync } = require('child_process');
const path = require('path');

const script = path.join(__dirname, 'build_cli.py');
const candidates = process.platform === 'win32' ? ['python', 'py'] : ['python3', 'python'];

for (const command of candidates) {
  const args = command === 'py' ? ['-3', script] : [script];
  const result = spawnSync(command, args, { stdio: 'inherit' });
  if (!result.error && result.status === 0) process.exit(0);
  if (result.error && result.error.code === 'ENOENT') continue;
  process.exit(result.status || 1);
}

console.error('Python 3 was not found. Install Python 3.10+ to build LLMWiki.');
process.exit(1);
