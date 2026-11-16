# Command center and owner policy controls (MND-16)

The header shows allocation, available, held and spent separately. Gateway
allocation comes from the backend instead of the simulated 5.000 constant.
Amounts are formatted as three-decimal USD-TDN; expandable developer detail shows
integer thousandths. The wallet view labels its last reconciled balance as owner
recorded. A newer mismatching observation cannot replace that earlier reconciled
value or be represented as an independently verified provider balance.

Owner policy writes carry expected_version and are serialized. Each successful
write refreshes authoritative state before the next write. No optimistic success
is displayed; conflicts/errors remain visible and refresh the state. Unknown
seller addresses are refused rather than filled with a placeholder. Adding a new
seller/address remains an owner backend operation; existing trusted vendors can be
toggled. Simulation retains its separate browser-only behavior and label.

Pause/resume waits for the gateway result and retains the dialog/error when the
request fails. Pause copy explicitly says dispatched payments may still complete.
The pause dialog supports Escape and returns focus to its opener on cancellation;
controls wait for an acknowledgment before claiming success.

Shared surfaces follow operating-system light/dark appearance, use quiet rounded
panels, improve secondary/status text contrast, and keep the command-center
controls at least 44px tall. The revocation slip respects reduced motion. This is
not a claim of a complete accessibility audit or measured WCAG conformance.

Live owner authentication depends on the separate MND-17 PR #4. This branch does
not duplicate its stream-cookie or credential handling. Until that PR lands, the
main cockpit's existing unauthenticated gateway attempts still fall back to the
labelled simulation; client tests exercise the owner contract with synthetic API
fixtures. Rebase and rerun the integrated flow after #4 merges.

Verification:

```text
python -m pytest backend/tests -q
ruff check backend scripts
```

From frontend:

```text
node --test tests/owner-controls.test.cjs
npx tsc --noEmit
npx next build
```

Rendered QA used the existing headless Chromium runtime on localhost:3100/console:
dark/light desktop at 1440px and light mobile at 390px, with reduced motion enabled.
Verified policy keyboard change, pause/resume acknowledgment, Escape, focus return,
44px slider target and no whole-page overflow. No relevant application console or
runtime errors were observed; the existing favicon 404 was excluded. Screenshots
are retained outside the repository. This run used the labelled simulation; live
owner-cookie integration awaits PR #4.
