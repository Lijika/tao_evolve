**observations**
- Implemented a 12% cross-die fraction guard in `Place-3D/thirdparty/DREAMPlace/dreamplace/concurrent_lol.py:305-328`; explicit non-unit weights still override.
- Parent `ecf5` matrix: 9/9 valid, official score `-6.5938`; all `bp_fe` cells negative, Ariane 1001 clamped to `-100`, Ariane 1003 `+130.39`.
- Contract written to `draft-2/contract.json` with the exact schema key set and no `reflection` field.

**supporting/counterevidence**
- Supports: `bp_fe` cross-die ratios are ~12.8–13.8%, while `bp`/Ariane are ~3.9–11.8%; the guard disables overweighting only for the harmed case.
- Counter: no same-stage parent/child cells exist, so the mixed result may be seed noise rather than a weight effect.

**competing explanation**
- Placement density, legalization, CTS, or routing may dominate. Uniformly damping all factors and fully reverting remain untested alternatives.

**next discriminating check**
- Run the official nine-cell matrix and compare matched post-GRT endpoint metrics against parent `ecf5`, checking `bp_fe` recovery, retained `bp`/Ariane gains, overflow, DRC, and legality.

Checks: `py_compile` passed; toy low/high-fraction and explicit-multiplier checks passed; 4 placement unit tests passed. Unresolved: critical endpoint migration and cross-process generalization remain unknown.