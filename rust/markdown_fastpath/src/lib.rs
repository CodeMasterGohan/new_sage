//! An opt-in Rust fast path for Sage document preprocessing.
//! Does not fetch pages or alter robots, throttling, authorization or chunks.
use pyo3::prelude::*;
use regex::Regex;
use std::sync::OnceLock;

type Unit = (String, String, Option<String>);
type Section = (Vec<String>, Vec<Unit>);

fn heading_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(#{1,6})\s+(.*?)\s*#*\s*$").unwrap())
}
fn fence_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(\s*)(`{3,}|~{3,})\s*([^\s`~]*)\s*$").unwrap())
}
fn setext1_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^=+\s*$").unwrap())
}
fn setext2_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^-+\s*$").unwrap())
}
fn current<'a>(sections: &'a mut Vec<Section>, stack: &[(usize, String)]) -> &'a mut Section {
    if sections.is_empty() {
        sections.push((stack.iter().map(|(_, title)| title.clone()).collect(), Vec::new()));
    }
    sections.last_mut().expect("section present")
}
fn flush_prose(buf: &mut Vec<String>, sections: &mut Vec<Section>, stack: &[(usize, String)]) {
    let value = buf.join("\n");
    let trimmed = value.trim();
    if !trimmed.is_empty() {
        current(sections, stack).1.push(("prose".into(), trimmed.into(), None));
    }
    buf.clear();
}

/// Structural Markdown parsing matching the Python heading/fence semantics.
/// Output contains data tuples only; Python constructs its own _Section and
/// _Unit models after extension return, retaining current public contracts.
#[pyfunction]
fn parse_sections(markdown: &str) -> Vec<Section> {
    let lines: Vec<&str> = markdown.lines().collect();
    let mut sections: Vec<Section> = Vec::new();
    let mut stack: Vec<(usize, String)> = Vec::new();
    let mut buf: Vec<String> = Vec::new();
    let mut code_lines: Vec<String> = Vec::new();
    let mut in_code = false;
    let mut fence_char = '\0';
    let mut fence_len: usize = 0;
    let mut code_lang: Option<String> = None;
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        let fence = if in_code { None } else { fence_re().captures(line) };
        if !in_code {
            if let Some(cap) = fence {
                flush_prose(&mut buf, &mut sections, &stack);
                let marker = cap.get(2).unwrap().as_str();
                in_code = true;
                fence_char = marker.chars().next().unwrap();
                fence_len = marker.len();
                let language = cap.get(3).map(|x| x.as_str()).unwrap_or("");
                code_lang = if language.is_empty() { None } else { Some(language.into()) };
                code_lines.clear();
                i += 1;
                continue;
            }
        }
        if in_code {
            let close = line.trim();
            if close.len() >= fence_len && close.chars().all(|c| c == fence_char) {
                current(&mut sections, &stack).1.push((
                    "code".into(), code_lines.join("\n"), code_lang.take()
                ));
                in_code = false;
            } else {
                code_lines.push(line.into());
            }
            i += 1;
            continue;
        }
        if let Some(cap) = heading_re().captures(line) {
            let level = cap.get(1).unwrap().as_str().len();
            if level <= 3 {
                let title = cap.get(2).unwrap().as_str().trim().to_owned();
                flush_prose(&mut buf, &mut sections, &stack);
                while stack.last().is_some_and(|x| x.0 >= level) {
                    stack.pop();
                }
                stack.push((level, title));
                sections.push((stack.iter().map(|(_, t)| t.clone()).collect(), Vec::new()));
            } else {
                buf.push(line.into());
            }
            i += 1;
            continue;
        }
        if i + 1 < lines.len() && !line.trim().is_empty() && !line.starts_with(' ') {
            if setext1_re().is_match(lines[i + 1]) {
                flush_prose(&mut buf, &mut sections, &stack);
                let title = line.trim().to_owned();
                stack.clear();
                stack.push((1, title.clone()));
                sections.push((vec![title], Vec::new()));
                i += 2;
                continue;
            }
            if setext2_re().is_match(lines[i + 1]) {
                flush_prose(&mut buf, &mut sections, &stack);
                let title = line.trim().to_owned();
                while stack.last().is_some_and(|x| x.0 >= 2) {
                    stack.pop();
                }
                stack.push((2, title));
                sections.push((stack.iter().map(|(_, t)| t.clone()).collect(), Vec::new()));
                i += 2;
                continue;
            }
        }
        buf.push(line.into());
        i += 1;
    }
    if in_code {
        current(&mut sections, &stack).1.push((
            "code".into(), code_lines.join("\n"), code_lang
        ));
    } else {
        flush_prose(&mut buf, &mut sections, &stack);
    }
    sections.into_iter().filter(|(bc, u)| !bc.is_empty() || !u.is_empty()).collect()
}

/// Matches Python's rstrip per line and collapse-3+-newlines normalization
/// for standard LF and CRLF documentation files. Unusual Unicode line
/// separators are routed to the existing Python implementation by the caller.
#[pyfunction]
fn normalize_markdown(md: &str) -> String {
    let mut out = String::with_capacity(md.len());
    for (index, line) in md.lines().enumerate() {
        if index != 0 {
            out.push('\n');
        }
        out.push_str(line.trim_end());
    }
    let mut collapsed = String::with_capacity(out.len());
    let mut newlines = 0usize;
    for c in out.chars() {
        if c == '\n' {
            newlines += 1;
            if newlines <= 2 {
                collapsed.push(c);
            }
        } else {
            newlines = 0;
            collapsed.push(c);
        }
    }
    collapsed.trim().to_string()
}

#[pymodule]
fn sage_markdown_fastpath(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_sections, m)?)?;
    m.add_function(wrap_pyfunction!(normalize_markdown, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn code_fence_and_headings() {
        let x = parse_sections("# Title\nparagraph\n\n```rust\nfn a() {}\n```\n## More\ntext");
        assert_eq!(x.len(), 2);
        assert_eq!(x[0].0, vec!["Title"]);
        assert_eq!(x[0].1[1].0, "code");
        assert_eq!(x[0].1[1].2.as_deref(), Some("rust"));
    }
    #[test]
    fn normalization() {
        assert_eq!(normalize_markdown(" a  \n\n\n b \n"), "a\n\nb");
    }
}
