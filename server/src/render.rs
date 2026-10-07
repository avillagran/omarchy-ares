//! The small Qt RichText renderer used for historical assistant event DTOs.
use crate::text;
use serde_json::Value;

pub fn tool_text(v: &Value) -> String {
    if let Some(s) = v.as_str() {
        if let Ok(decoded) = serde_json::from_str::<Value>(s.trim()) {
            if decoded.is_object()
                && ["output", "_multimodal", "text_summary"]
                    .iter()
                    .any(|k| decoded.get(k).is_some())
            {
                return tool_text(&decoded);
            }
        }
        return s.to_owned();
    }
    if v.is_object() {
        if v["_multimodal"] == true {
            if let Some(content) = v["content"].as_array() {
                if !text(v, "text_summary").is_empty() {
                    return text(v, "text_summary").to_owned();
                }
                let parts: Vec<_> = content
                    .iter()
                    .filter(|p| p["type"] == "text" && !text(p, "text").trim().is_empty())
                    .map(|p| text(p, "text"))
                    .collect();
                return if parts.is_empty() {
                    "[multimodal tool result]".into()
                } else {
                    parts.join("\n")
                };
            }
        }
        if let Some(output) = v.get("output") {
            return tool_text(output);
        }
    }
    serde_json::to_string_pretty(v).unwrap_or_default()
}
pub fn tool_args(v: &Value) -> String {
    if let Some(s) = v.as_str() {
        s.to_owned()
    } else if !text(v, "command").trim().is_empty() {
        text(v, "command").trim().to_owned()
    } else {
        serde_json::to_string_pretty(v).unwrap_or_default()
    }
}
fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}
fn delimited(s: &str, marker: &str, tag: &str, boundary: bool) -> String {
    let mut out = String::new();
    let mut pos = 0;
    while let Some(offset) = s[pos..].find(marker) {
        let start = pos + offset;
        let body = start + marker.len();
        let Some(end_offset) = s[body..].find(marker) else {
            break;
        };
        let end = body + end_offset;
        let word = |c: char| c.is_alphanumeric() || c == '_' || marker.contains(c);
        let before = s[..start].chars().next_back();
        let after = s[end + marker.len()..].chars().next();
        let valid = end > body
            && !s[body..end].contains(marker.chars().next().unwrap())
            && (!boundary
                || (!s[body..end].contains('\n')
                    && !before.is_some_and(word)
                    && !after.is_some_and(word)));
        if valid {
            out.push_str(&s[pos..start]);
            out.push_str(&format!("<{tag}>{}</{tag}>", &s[body..end]));
            pos = end + marker.len();
        } else {
            out.push_str(&s[pos..body]);
            pos = body;
        }
    }
    out.push_str(&s[pos..]);
    out
}
fn inline(s: &str) -> String {
    let s = escape(s);
    let mut escaped = String::new();
    let mut spans = Vec::new();
    let mut pos = 0;
    while let Some(offset) = s[pos..].find('`') {
        let start = pos + offset;
        let Some(end_offset) = s[start + 1..].find('`') else {
            break;
        };
        let end = start + 1 + end_offset;
        escaped.push_str(&s[pos..start]);
        if end > start + 1 && !s[start + 1..end].contains('\n') {
            escaped.push_str(&format!("\0CODE{}\0", spans.len()));
            spans.push(format!(
                "<code style=\"font-family:monospace; color:__ARES_ACCENT__\">{}</code>",
                &s[start + 1..end]
            ));
        } else {
            escaped.push_str(&s[start..=end]);
        }
        pos = end + 1;
    }
    escaped.push_str(&s[pos..]);
    for (marker, tag, boundary) in [
        ("**", "b", false),
        ("*", "i", true),
        ("__", "b", false),
        ("_", "i", true),
        ("~~", "s", false),
    ] {
        escaped = delimited(&escaped, marker, tag, boundary);
    }
    let s = escaped;
    let mut escaped = String::new();
    let mut pos = 0;
    while let Some(offset) = s[pos..].find('[') {
        let start = pos + offset;
        let Some(mid_offset) = s[start + 1..].find("](") else {
            break;
        };
        let mid = start + 1 + mid_offset;
        let Some(end_offset) = s[mid + 2..].find(')') else {
            break;
        };
        let end = mid + 2 + end_offset;
        let url = &s[mid + 2..end];
        let label = &s[start + 1..mid];
        escaped.push_str(&s[pos..start]);
        if label.is_empty() || url.is_empty() || url.chars().any(char::is_whitespace) {
            escaped.push_str(&s[start..=end]);
        } else if ["http://", "https://", "mailto:"]
            .iter()
            .any(|p| url.to_lowercase().starts_with(p))
        {
            escaped.push_str(&format!(
                "<a href=\"{url}\"><font color=\"__ARES_ACCENT__\">{label}</font></a>"
            ));
        } else {
            escaped.push_str(label);
        }
        pos = end + 1;
    }
    escaped.push_str(&s[pos..]);
    for (i, span) in spans.iter().enumerate() {
        escaped = escaped.replace(&format!("\0CODE{i}\0"), span);
    }
    escaped
}
fn list_item(line: &str, ordered: bool) -> Option<&str> {
    if ordered {
        let n = line.bytes().take_while(u8::is_ascii_digit).count();
        if n == 0 || !matches!(line.as_bytes().get(n), Some(b'.' | b')')) {
            return None;
        }
        let rest = &line[n + 1..];
        if rest.starts_with(char::is_whitespace) {
            Some(rest.trim_start())
        } else {
            None
        }
    } else {
        let rest = line.strip_prefix('-').or_else(|| line.strip_prefix('*'))?;
        if rest.starts_with(char::is_whitespace) {
            Some(rest.trim_start())
        } else {
            None
        }
    }
}
pub fn markdown(source: &str) -> String {
    if source.trim().is_empty() {
        return String::new();
    }
    let source = source.replace("\r\n", "\n").replace('\r', "\n");
    let lines: Vec<_> = source.split('\n').collect();
    let mut out = Vec::new();
    let mut paragraph = Vec::new();
    let mut i = 0;
    fn flush(out: &mut Vec<String>, p: &mut Vec<&str>) {
        if !p.is_empty() {
            out.push(format!("<p>{}</p>", inline(&p.join(" "))));
            p.clear();
        }
    }
    while i < lines.len() {
        let s = lines[i].trim();
        if s.is_empty() {
            flush(&mut out, &mut paragraph);
            i += 1;
            continue;
        }
        if s.starts_with("```") {
            flush(&mut out, &mut paragraph);
            i += 1;
            let start = i;
            while i < lines.len() && !lines[i].trim().starts_with("```") {
                i += 1;
            }
            out.push(format!("<table width=\"100%\" style=\"background-color:__ARES_CODE_BG__\" cellspacing=\"0\" cellpadding=\"4\"><tr><td><pre style=\"margin:0; font-family:monospace\">{}</pre></td></tr></table>",escape(&lines[start..i].join("\n"))));
            i += 1;
            continue;
        }
        let level = s.bytes().take_while(|c| *c == b'#').count();
        if (1..=6).contains(&level) && s[level..].starts_with(char::is_whitespace) {
            flush(&mut out, &mut paragraph);
            out.push(format!(
                "<h{level}>{}</h{level}>",
                inline(s[level..].trim_start())
            ));
            i += 1;
            continue;
        }
        let chars: Vec<_> = s.chars().filter(|c| !c.is_whitespace()).collect();
        if chars.len() >= 3
            && ['-', '*', '_'].contains(&chars[0])
            && chars.iter().all(|c| *c == chars[0])
        {
            flush(&mut out, &mut paragraph);
            out.push("<hr/>".into());
            i += 1;
            continue;
        }
        if s.starts_with('|') && i + 1 < lines.len() && {
            let next = lines[i + 1].trim();
            next.starts_with('|')
                && next.ends_with('|')
                && next.len() > 2
                && next.chars().all(|c| c.is_whitespace() || ":|-".contains(c))
        } {
            flush(&mut out, &mut paragraph);
            let cells = |line: &str| {
                line.trim()
                    .trim_matches('|')
                    .split('|')
                    .map(|c| format!("<td>{}</td>", inline(c.trim())))
                    .collect::<String>()
            };
            let mut table=format!("<table width=\"100%\" cellspacing=\"0\" cellpadding=\"3\"><tr bgcolor=\"__ARES_SURFACE__\">{}</tr>",cells(s));
            i += 2;
            while i < lines.len() && lines[i].trim().starts_with('|') {
                table.push_str(&format!("<tr>{}</tr>", cells(lines[i])));
                i += 1;
            }
            table.push_str("</table>");
            out.push(table);
            continue;
        }
        let ordered = list_item(s, true).is_some();
        if ordered || list_item(s, false).is_some() {
            flush(&mut out, &mut paragraph);
            let tag = if ordered { "ol" } else { "ul" };
            let mut list = format!("<{tag}>");
            while i < lines.len() {
                let Some(item) = list_item(lines[i].trim(), ordered) else {
                    break;
                };
                list.push_str(&format!("<li>{}</li>", inline(item)));
                i += 1;
            }
            list.push_str(&format!("</{tag}>"));
            out.push(list);
            continue;
        }
        if s.starts_with('>') {
            flush(&mut out, &mut paragraph);
            let mut quotes = Vec::new();
            while i < lines.len() {
                let Some(q) = lines[i].trim().strip_prefix('>') else {
                    break;
                };
                let q = if q.starts_with(char::is_whitespace) {
                    &q[q.chars().next().unwrap().len_utf8()..]
                } else {
                    q
                };
                quotes.push(q);
                i += 1;
            }
            out.push(format!(
                "<p style=\"margin-left:10; font-style:italic; color:__ARES_FG_DIM__\">{}</p>",
                inline(&quotes.join(" "))
            ));
            continue;
        }
        paragraph.push(s);
        i += 1;
    }
    flush(&mut out, &mut paragraph);
    out.join("\n")
}
