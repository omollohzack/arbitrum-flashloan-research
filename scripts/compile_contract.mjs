// scripts/compile_contract.mjs — verified deployable compile for FlashLoanArbitrage.sol
//
// Usage:
//   mkdir -p /tmp/solc-check && cd /tmp/solc-check && npm install solc@0.8.20
//   NODE_PATH=/tmp/solc-check/node_modules node scripts/compile_contract.mjs
//
// Settings are the verified deployable configuration (solc 0.8.20):
// via-IR + optimizer runs=1 -> 22,720 bytes deployed (< 24,576 limit),
// 0 errors, 0 warnings. A standard (non-via-IR) compile also succeeds
// after the v3.6 swap-dispatch split, but produces ~27.2 KB, which is
// NOT deployable — this script therefore asserts the size limit.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
let solc;
try {
  solc = require('solc');
} catch {
  console.error('Missing dependency: run `npm install solc@0.8.20` and set NODE_PATH to its node_modules.');
  process.exit(2);
}
if (!solc.version().startsWith('0.8.20')) {
  console.error(`Expected solc 0.8.20, got ${solc.version()}`);
  process.exit(2);
}

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(repoRoot, 'contracts', 'FlashLoanArbitrage.sol'), 'utf8');
const input = {
  language: 'Solidity',
  sources: { 'FlashLoanArbitrage.sol': { content: source } },
  settings: {
    viaIR: true,
    optimizer: { enabled: true, runs: 1 },
    outputSelection: { '*': { '*': ['evm.deployedBytecode.object', 'abi'] } },
  },
};
const out = JSON.parse(solc.compile(JSON.stringify(input)));
const errors = (out.errors || []).filter((e) => e.severity === 'error');
const warnings = (out.errors || []).filter((e) => e.severity === 'warning');
for (const e of [...errors, ...warnings]) console.log(e.formattedMessage);
if (errors.length) {
  console.error(`COMPILE FAILED with ${errors.length} error(s)`);
  process.exit(1);
}
const contract = out.contracts['FlashLoanArbitrage.sol'].FlashLoanArbitrage;
const sizeBytes = contract.evm.deployedBytecode.object.length / 2;
console.log(`FlashLoanArbitrage deployed size: ${sizeBytes} bytes (limit 24576)`);
if (sizeBytes > 24576) {
  console.error('FAIL: contract exceeds the 24,576-byte deploy limit');
  process.exit(1);
}
console.log(`COMPILE OK — 0 errors, ${warnings.length} warning(s), deployable size`);
