//! Plugin config, read once at boot from the SD card.
//!
//! sd:/ultimate/super-cpu/config.toml
//!
//! ```toml
//! pc_ip = "192.168.1.50"
//! udp_port = 4243
//! bot_port = 2
//! ```
//!
//! Only flat `key = value` lines are supported. Anything missing falls back
//! to the defaults below, and a missing file writes a template you can edit.
//!
//! `bot_port` (1-based) is the port the PC drives while a bridge is running.
//! The bridge's `--bot-port` has to match (it warns if it doesn't). Optional
//! `fim_player_idx` overrides which final-input-mapping player index counts
//! as the bot; leave it out unless docs/hooks.md says you need it.
//! `cpu_probe = 0` turns off the read-only CPU controls probe.
//! `drive_cpu = true` makes the bot's port a CPU slot the PC drives (no
//! controller needed, the card survives rematches, docs/hooks.md section 3).
//! `cpu_controller_index` overrides which `controller_index` the CPU hook
//! treats as the bot; leave it out unless the match-start log is wrong.
//! `auto_menu = true` lets the PC press menu buttons (on `menu_npad`, default the lowest connected controller)
//! outside matches (SSB-25, off by default).
//! `force_fighter = true` makes the ports in `force_ports` (1-based, `[2]` or
//! `2,3`) play green R.O.B. with costume `force_costume` (SSB-24, off by
//! default, game 13.0.4 only). `force_copy_hook = false` skips the second
//! (inline) hook if it crashes on boot.

pub const CONFIG_DIR: &str = "sd:/ultimate/super-cpu";
pub const CONFIG_PATH: &str = "sd:/ultimate/super-cpu/config.toml";

#[derive(Clone, Debug)]
pub struct Config {
    pub pc_ip: String,
    pub udp_port: u16,
    /// 1-based port the bridge drives.
    pub bot_port: u8,
    /// player_idx in the final input mapping hook that means the bot. None
    /// means bot_port - 1.
    pub fim_player_idx: Option<i32>,
    /// Install the read-only set_cpu_controls probe (docs/hooks.md).
    pub cpu_probe: bool,
    /// The bot's port is a CPU: write the PC's inputs into its CPU controls
    /// instead of the final input mapping (docs/hooks.md section 3).
    pub drive_cpu: bool,
    /// controller_index of the bot's CPU, if auto-detection needs help.
    pub cpu_controller_index: Option<i32>,
    /// Which controller (npad number, 0-based) gets the menu presses.
    /// None: the lowest connected one, usually your own controller. The bot's
    /// port can be a CPU card with no controller (drive_cpu), and any
    /// controller can press A on results or + on the CSS.
    pub menu_npad: Option<u32>,
    /// Install the menu press hook and apply kind 8 packets (docs/hooks.md).
    pub auto_menu: bool,
    /// Force R.O.B. on `force_ports` (docs/hooks.md section 6).
    pub force_fighter: bool,
    /// 1-based ports to force. Can include the opponent CPU's port.
    pub force_ports: Vec<u8>,
    /// Costume index forced (green R.O.B. is 5).
    pub force_costume: u8,
    /// Install the record copy hook as well as the decided-fighter hook.
    pub force_copy_hook: bool,
    /// Apply drill resets (kind 9) from the PC (SSB-32, docs/drills.md).
    pub drills: bool,
    /// Allow drill resets outside training mode too.
    pub drills_in_versus: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self { pc_ip: "192.168.1.50".into(), udp_port: 4243, bot_port: 2, fim_player_idx: None, cpu_probe: true, auto_menu: false, drive_cpu: false, cpu_controller_index: None, menu_npad: None,
            force_fighter: false, force_ports: vec![2], force_costume: 5, force_copy_hook: true,
            drills: false, drills_in_versus: false }
    }
}

impl Config {
    pub fn bot_player_idx(&self) -> i32 {
        self.fim_player_idx.unwrap_or(self.bot_port as i32 - 1)
    }
}

pub fn template(cfg: &Config) -> String {
    format!(
        "# super-cpu plugin config\npc_ip = \"{}\"\nudp_port = {}\n# port the PC bot drives (1-based); match it with the bridge's --bot-port\nbot_port = {}\n\
         # the PC drives a CPU card on bot_port instead of a controller (docs/hooks.md section 3)\n\
         # drive_cpu = true\n\
         # force green R.O.B. on these ports without touching the CSS (game 13.0.4 only)\n\
         # force_fighter = true\n\
         # force_ports = [2]\n\
         # force_costume = 5\n\
         # drill resets from the PC (training mode; docs/drills.md)\n\
         # drills = true\n",
        cfg.pc_ip, cfg.udp_port, cfg.bot_port
    )
}

pub fn load() -> Config {
    let mut cfg = Config::default();
    match std::fs::read_to_string(CONFIG_PATH) {
        Ok(text) => parse_into(&text, &mut cfg),
        Err(_) => {
            let _ = std::fs::create_dir_all(CONFIG_DIR);
            let _ = std::fs::write(CONFIG_PATH, template(&cfg));
            crate::log::log(format!("no config found, wrote a template to {}", CONFIG_PATH));
        }
    }
    cfg
}

/// `[2]`, `2,3` or `"2 3"` as sorted, deduplicated 1-based ports. Anything
/// outside 1 to 8 is dropped.
pub fn parse_ports(value: &str) -> Vec<u8> {
    let mut out: Vec<u8> = value
        .split(|c: char| !c.is_ascii_digit())
        .filter_map(|s| s.parse::<u8>().ok())
        .filter(|p| (1..=8).contains(p))
        .collect();
    out.sort_unstable();
    out.dedup();
    out
}

fn parse_into(text: &str, cfg: &mut Config) {
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else { continue };
        let value = value.trim().trim_matches('"');
        match key.trim() {
            "pc_ip" => cfg.pc_ip = value.to_string(),
            "udp_port" => {
                if let Ok(p) = value.parse() {
                    cfg.udp_port = p;
                }
            }
            "bot_port" => match value.parse::<u8>() {
                Ok(p) if (1..=8).contains(&p) => cfg.bot_port = p,
                _ => crate::log::log(format!("bot_port must be 1 to 8, got {value:?}, keeping {}", cfg.bot_port)),
            },
            "drive_cpu" => cfg.drive_cpu = matches!(value, "1" | "true" | "on"),
            "cpu_controller_index" => {
                if let Ok(i) = value.parse() {
                    cfg.cpu_controller_index = Some(i);
                }
            }
            "auto_menu" => cfg.auto_menu = matches!(value, "1" | "true" | "on"),
            "menu_npad" => {
                if let Ok(n) = value.parse() {
                    cfg.menu_npad = Some(n);
                }
            }
            "force_fighter" => cfg.force_fighter = matches!(value, "1" | "true" | "on"),
            "force_copy_hook" => cfg.force_copy_hook = !matches!(value, "0" | "false" | "off"),
            "drills" => cfg.drills = matches!(value, "1" | "true" | "on"),
            "drills_in_versus" => cfg.drills_in_versus = matches!(value, "1" | "true" | "on"),
            "force_ports" => {
                let ports = parse_ports(value);
                if ports.is_empty() {
                    crate::log::log(format!("force_ports needs ports 1 to 8, got {value:?}, keeping {:?}", cfg.force_ports));
                } else {
                    cfg.force_ports = ports;
                }
            }
            "force_costume" => match value.parse::<u8>() {
                Ok(c) if c <= 15 => cfg.force_costume = c,
                _ => crate::log::log(format!("force_costume must be 0 to 15, got {value:?}, keeping {}", cfg.force_costume)),
            },
            "cpu_probe" => cfg.cpu_probe = !matches!(value, "0" | "false" | "off"),
            "fim_player_idx" => {
                if let Ok(i) = value.parse() {
                    cfg.fim_player_idx = Some(i);
                }
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_and_old_files() {
        let mut c = Config::default();
        parse_into("pc_ip = \"10.0.0.2\"\nudp_port = 5000\n", &mut c);
        assert_eq!((c.pc_ip.as_str(), c.udp_port, c.bot_port), ("10.0.0.2", 5000, 2));
        assert_eq!(c.bot_player_idx(), 1);
    }

    #[test]
    fn bot_port_and_override() {
        let mut c = Config::default();
        parse_into("bot_port = 1\n", &mut c);
        assert_eq!((c.bot_port, c.bot_player_idx()), (1, 0));
        parse_into("bot_port = 9\nbot_port = x\n", &mut c);
        assert_eq!(c.bot_port, 1, "bad values are ignored");
        parse_into("fim_player_idx = 4\n", &mut c);
        assert_eq!(c.bot_player_idx(), 4);
        assert!(c.cpu_probe);
        parse_into("cpu_probe = 0\n", &mut c);
        assert!(!c.cpu_probe);
        assert!(!c.auto_menu);
        parse_into("auto_menu = true\n", &mut c);
        assert!(c.auto_menu);
    }

    #[test]
    fn drive_cpu_keys() {
        let mut c = Config::default();
        assert!(!c.drive_cpu && c.cpu_controller_index.is_none(), "off by default");
        parse_into("drive_cpu = true\ncpu_controller_index = 3\n", &mut c);
        assert!(c.drive_cpu);
        assert_eq!(c.cpu_controller_index, Some(3));
        parse_into("drive_cpu = off\ncpu_controller_index = x\n", &mut c);
        assert!(!c.drive_cpu);
        assert_eq!(c.cpu_controller_index, Some(3));
    }

    #[test]
    fn force_keys() {
        let mut c = Config::default();
        assert!(!c.force_fighter && c.force_ports == vec![2] && c.force_costume == 5 && c.force_copy_hook);
        parse_into("force_fighter = true\nforce_ports = [1, 2]\nforce_costume = 3\nforce_copy_hook = off\n", &mut c);
        assert!(c.force_fighter && !c.force_copy_hook);
        assert_eq!((c.force_ports.clone(), c.force_costume), (vec![1, 2], 3));
        parse_into("force_ports = \"4\"\nforce_costume = 99\n", &mut c);
        assert_eq!((c.force_ports.clone(), c.force_costume), (vec![4], 3));
        parse_into("force_ports = []\nforce_ports = 0,9\n", &mut c);
        assert_eq!(c.force_ports, vec![4], "empty or invalid lists are ignored");
    }

    #[test]
    fn port_lists() {
        assert_eq!(parse_ports("[2]"), vec![2]);
        assert_eq!(parse_ports("3,2, 3"), vec![2, 3]);
        assert_eq!(parse_ports("2 1"), vec![1, 2]);
        assert_eq!(parse_ports("0, 9, 300, x"), Vec::<u8>::new());
    }

    #[test]
    fn template_parses_back() {
        let d = Config::default();
        let mut c = Config { pc_ip: String::new(), udp_port: 0, bot_port: 1, fim_player_idx: None, cpu_probe: false, auto_menu: false, drive_cpu: true, cpu_controller_index: Some(9), menu_npad: Some(3),
            force_fighter: false, force_ports: vec![], force_costume: 0, force_copy_hook: false,
            drills: false, drills_in_versus: false };
        parse_into(&template(&d), &mut c);
        assert_eq!((c.pc_ip, c.udp_port, c.bot_port), (d.pc_ip, d.udp_port, d.bot_port));
    }
}
