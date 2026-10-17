# Resume input admission

An operator can disconnect immediately after saving a durable Resume request. The
claim transaction now admits held, unconsumed input from that selected unfinished
tree along with its epoch and work states. Exact offered attempt identities scope
the update; terminal branches, other trees and canonical receipts are excluded.
The workspace keeps global unholding only for legacy sessions without durable work.

The regression closes/reopens state after request persistence, injects an input
update failure and proves epoch/request/input rollback, then verifies selected-tree
admission on retry. A route regression prevents unrelated held input from being
released. Request-only continuation also exercises the consolidation lifecycle.

55 ownership, service, chat and recovery-route tests passed in 15.54 seconds.
Log `/home/andy/.cache/pt/foundations-input-claim-final.log`, SHA256
`d03d4faf5f5ad857149cc02af271ba11d2d6a7cde16e2b741f86ed381cc16493`.
Pyright reports zero errors; Ruff, formatting and diff checks pass. Independent
GPT-6.1-Sol scope/concurrency review reports no medium-or-higher findings.
