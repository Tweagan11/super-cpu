//! Version guard (SSB-10). Data from a different game build or mod set
//! must never mix with the training set.

/// Set this to the Ultimate version on your Switch before collecting data.
pub const EXPECTED_GAME_VERSION: &str = "13.0.x";

/// Hash of the active mod set. Fill in once the mod list is frozen.
pub const EXPECTED_MOD_HASH: &str = "unset";

// TODO(SSB-10): read the running game version and mod set at boot and
// disable logging and bot control if either doesn't match.
