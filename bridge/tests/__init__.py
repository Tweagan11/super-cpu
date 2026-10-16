# Most tests end matches by letting state stop with stocks left (no one is
# out of stocks in the synthetic frames). On hardware that's a pause and the
# match stays open for PAUSE_TIMEOUT_S; here it ends after END_TIMEOUT_S like
# before. tests/test_matches.py PauseTests restores the real value.
import bridge.matches as _matches

_matches.PAUSE_TIMEOUT_S = 0.0
