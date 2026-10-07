use crate::{
    now, require,
    store::{truthy, Store},
    text, truncate, Result,
};
use rusqlite::params;
use serde_json::{json, Value};
use std::{collections::BTreeSet, fs, path::Path};

pub(crate) fn json_read(path: &Path) -> Value {
    fs::read(path)
        .ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .filter(Value::is_object)
        .unwrap_or(json!({}))
}
pub(crate) fn json_write(path: &Path, value: &Value) {
    let _ = (|| -> Result<()> {
        fs::create_dir_all(path.parent().ok_or("sidecar has no parent")?)?;
        let tmp = path.with_file_name(format!(
            "{}.tmp",
            path.file_name().unwrap().to_string_lossy()
        ));
        fs::write(&tmp, format!("{}\n", serde_json::to_string_pretty(value)?))?;
        fs::rename(tmp, path)?;
        Ok(())
    })();
}
pub fn profile_model_pair(profile: &Path) -> Option<(String, String)> {
    let bytes = fs::read(profile.join("config.yaml")).ok()?;
    let content = String::from_utf8_lossy(&bytes);
    let mut in_model = false;
    let mut model = String::new();
    let mut provider = String::new();
    for line in content.lines() {
        if !line.is_empty() && !line.starts_with([' ', '\t']) {
            in_model = line.split(':').next()?.trim() == "model";
            continue;
        }
        if !in_model {
            continue;
        }
        if let Some(v) = line.trim().strip_prefix("default:") {
            model = v.trim().trim_matches(['\'', '"']).into();
        } else if let Some(v) = line.trim().strip_prefix("provider:") {
            provider = v.trim().trim_matches(['\'', '"']).into();
        }
    }
    if model.is_empty() {
        None
    } else {
        Some((provider, model))
    }
}
pub fn write_profile_model(profile: &Path, model: &str, provider: &str) {
    let config = profile.join("config.yaml");
    let Ok(bytes) = fs::read(&config) else {
        return;
    };
    let content = String::from_utf8_lossy(&bytes);
    let mut lines: Vec<String> = content.lines().map(str::to_owned).collect();
    let top = |s: &str| !s.is_empty() && !s.starts_with([' ', '\t']);
    let quoted = |s: &str| format!("'{}'", s.replace('\'', "''"));
    let default = format!("  default: {}", quoted(model));
    let provider = format!("  provider: {}", quoted(provider));
    if let Some(index) = lines
        .iter()
        .position(|s| top(s) && s.split(':').next().unwrap_or("").trim() == "model")
    {
        let end = (index + 1..lines.len())
            .find(|&i| top(&lines[i]))
            .unwrap_or(lines.len());
        // Replace existing fields before inserting missing fields so offsets remain valid.
        let d = (index + 1..end).find(|&i| lines[i].trim().starts_with("default:"));
        let p = (index + 1..end).find(|&i| lines[i].trim().starts_with("provider:"));
        if let Some(i) = d {
            lines[i] = default.clone();
        }
        if let Some(i) = p {
            lines[i] = provider.clone();
        }
        if d.is_none() {
            lines.insert(index + 1, default);
        }
        if p.is_none() {
            lines.insert((index + 2).min(lines.len()), provider);
        }
    } else {
        lines.splice(0..0, ["model:".into(), default, provider, String::new()]);
    }
    let _ = fs::write(config, lines.join("\n") + "\n");
}
impl Store {
    pub fn models_catalog(&self) -> Result<Vec<Value>> {
        let mut pairs = BTreeSet::new();
        if let Ok(entries) = fs::read_dir(self.hermes_home.join("profiles")) {
            for entry in entries.flatten() {
                if entry.path().is_dir() {
                    if let Some(pair) = profile_model_pair(&entry.path()) {
                        pairs.insert(pair);
                    }
                }
            }
        }
        for sql in [
            "SELECT DISTINCT provider,model FROM bots WHERE model!=''",
            "SELECT provider,model FROM known_models",
        ] {
            for row in self.rows(sql, [])? {
                pairs.insert((
                    text(&row, "provider").to_owned(),
                    text(&row, "model").to_owned(),
                ));
            }
        }
        Ok(pairs
            .into_iter()
            .map(|(provider, model)| json!({"provider":provider,"model":model}))
            .collect())
    }
    pub fn update_peer_preferences(&self, peer: &str, name: &str, color: &str) -> Result<Value> {
        let peer = require(peer, "peer", 128)?;
        let name = require(name, "peer name", 160)?;
        if !roles().contains(&color) {
            return Err("unsupported peer color role".into());
        }
        let result = self.transaction(|s| {
            s.one(
                "SELECT id FROM peers WHERE id=?",
                [&peer],
                "peer does not exist",
            )?;
            s.db.execute(
                "UPDATE peers SET name=?,color_role=?,updated_at=? WHERE id=?",
                params![name, color, now(), peer],
            )?;
            s.dto(
                "peers",
                &s.one(
                    "SELECT * FROM peers WHERE id=?",
                    [&peer],
                    "peer does not exist",
                )?,
            )
        })?;
        let path = self.hermes_home.join("ares-peers.json");
        let mut sidecar = json_read(&path);
        sidecar[&peer] = json!({"name":name,"color_role":color});
        json_write(&path, &sidecar);
        Ok(result)
    }
    pub fn update_bot(&self, bot: &str, values: &Value) -> Result<Value> {
        let row = self.one("SELECT * FROM bots WHERE id=?", [bot], "bot not found")?;
        let next = |key: &str, column: &str| {
            values
                .get(key)
                .filter(|v| !v.is_null())
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| v.to_string())
                        .trim()
                        .to_owned()
                })
                .unwrap_or_else(|| text(&row, column).to_owned())
        };
        let name = next("name", "name");
        let description = next("description", "description");
        let shape = next("faceShape", "face_shape");
        let color = next("colorRole", "color_role");
        let model = next("model", "model");
        let provider = next("provider", "provider");
        let enabled = values
            .get("enabled")
            .filter(|v| !v.is_null())
            .map(|v| i64::from(truthy(v)))
            .unwrap_or(row["enabled"].as_i64().unwrap_or(1));
        if name.is_empty() {
            return Err("bot name must not be empty".into());
        }
        if !["oval", "square", "triangle", "diamond", "circle", "hexagon"].contains(&shape.as_str())
        {
            return Err("unsupported bot face shape".into());
        }
        if !roles().contains(&color.as_str()) {
            return Err("unsupported bot color role".into());
        }
        if model.chars().count() > 160 || provider.chars().count() > 160 {
            return Err("model or provider too long".into());
        }
        let changed = model != text(&row, "model") || provider != text(&row, "provider");
        let result = self.transaction(|s| {
            s.db.execute("UPDATE bots SET name=?,description=?,face_shape=?,color_role=?,model=?,provider=?,enabled=? WHERE id=?",params![truncate(&name,160),truncate(&description,500),shape,color,model,provider,enabled,bot])?;
            if !model.is_empty() { s.db.execute("INSERT OR IGNORE INTO known_models(provider,model) VALUES(?,?)",params![provider,model])?; }
            let unbound = if changed {
                let rows = s.rows("SELECT stored_session_id,runtime_session_id FROM session_bindings WHERE bot_id=?",[bot])?;
                s.db.execute("DELETE FROM session_bindings WHERE bot_id=?",[bot])?;
                rows.iter().map(|r| json!({"storedSessionId":text(r,"stored_session_id"),"runtimeSessionId":text(r,"runtime_session_id")})).collect::<Vec<_>>()
            } else { vec![] };
            let mut dto = s.dto("bots",&s.one("SELECT * FROM bots WHERE id=?",[bot],"bot not found")?)?;
            dto["unboundSessions"] = json!(unbound); Ok(dto)
        })?;
        if text(&row, "node_id") == "local" {
            let profile = self
                .hermes_home
                .join("profiles")
                .join(text(&row, "profile"));
            if ["model", "provider"]
                .iter()
                .any(|k| values.get(k).is_some_and(|v| !v.is_null()))
            {
                write_profile_model(&profile, &model, &provider);
            }
            json_write(
                &profile.join("ares.json"),
                &json!({"name":result["name"],"description":result["description"],"model":model,"provider":provider,"face_shape":shape,"color_role":color}),
            );
        }
        Ok(result)
    }
}
fn roles() -> [&'static str; 6] {
    ["accent", "success", "warning", "urgent", "info", "muted"]
}
