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
}

impl Default for Config {
    fn default() -> Self {
        Self { pc_ip: "192.168.1.50".into(), udp_port: 4243, bot_port: 2, fim_player_idx: None, cpu_probe: true }
    }
}

impl Config {
    pub fn bot_player_idx(&self) -> i32 {
        self.fim_player_idx.unwrap_or(self.bot_port as i32 - 1)
    }
}

pub fn template(cfg: &Config) -> String {
    format!(
        "# super-cpu plugin config\npc_ip = \"{}\"\nudp_port = {}\n# port the PC bot drives (1-based); match it with the bridge's --bot-port\nbot_port = {}\n",
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
    }

    #[test]
    fn template_parses_back() {
        let d = Config::default();
        let mut c = Config { pc_ip: String::new(), udp_port: 0, bot_port: 1, fim_player_idx: None, cpu_probe: false };
        parse_into(&template(&d), &mut c);
        assert_eq!((c.pc_ip, c.udp_port, c.bot_port), (d.pc_ip, d.udp_port, d.bot_port));
    }
}
