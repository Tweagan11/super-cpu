//! Plugin config, read once at boot from the SD card.
//!
//! sd:/ultimate/super-cpu/config.toml
//!
//!     pc_ip = "192.168.1.50"
//!     udp_port = 4243
//!
//! Only flat `key = value` lines are supported. Anything missing falls back
//! to the defaults below, and a missing file writes a template you can edit.

pub const CONFIG_DIR: &str = "sd:/ultimate/super-cpu";
pub const CONFIG_PATH: &str = "sd:/ultimate/super-cpu/config.toml";

#[derive(Clone, Debug)]
pub struct Config {
    pub pc_ip: String,
    pub udp_port: u16,
}

impl Default for Config {
    fn default() -> Self {
        Self { pc_ip: "192.168.1.50".into(), udp_port: 4243 }
    }
}

pub fn load() -> Config {
    let mut cfg = Config::default();
    match std::fs::read_to_string(CONFIG_PATH) {
        Ok(text) => parse_into(&text, &mut cfg),
        Err(_) => {
            let _ = std::fs::create_dir_all(CONFIG_DIR);
            let template = format!(
                "# super-cpu plugin config\npc_ip = \"{}\"\nudp_port = {}\n",
                cfg.pc_ip, cfg.udp_port
            );
            let _ = std::fs::write(CONFIG_PATH, template);
            println!("[super-cpu] no config found, wrote a template to {}", CONFIG_PATH);
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
            _ => {}
        }
    }
}
