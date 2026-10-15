# training

| Story | File | Status |
| --- | --- | --- |
| SSB-14 Feature pipeline | `features.py` | not started |
| SSB-15 Action space | `actions.py` | stick and c-stick tables done, buttons pending |
| SSB-16 Behavior cloning | `train_bc.py` | not started |
| SSB-18 DAgger | `dagger.py` | not started |
| SSB-21 RL fine-tune | `rl.py` | not started |

Rules that keep data usable:

- Raw logs are never edited. Features are always derived.
- The feature code used in training is the same code the bridge runs live.
- Train with input delay randomized from 0 to the worst case measured in SSB-7.
