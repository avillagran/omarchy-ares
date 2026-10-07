//! Ares native app — proof of concept.
//!
//! One window: room list (left), Hermes TUI in a VTE terminal per room
//! (center), native image/video artifact strip (bottom). No HTML anywhere:
//! the chat surface IS the Hermes TUI; files render natively via gtk.

use gtk4::{glib, prelude::*};
use serde_json::Value;
use std::{cell::RefCell, collections::HashMap, path::PathBuf, rc::Rc};
use vte4::prelude::*;

const APP_ID: &str = "cl.villagran.ares";
const DEFAULT_ENGINE: &str = "http://127.0.0.1:9420";

type RoomData = (Option<Value>, Vec<Value>, Option<String>);

fn state_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("ARES_STATE_DIR") {
        return PathBuf::from(dir);
    }
    PathBuf::from(
        std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default(),
    )
    .join(".local/state/omarchy/ares")
}

fn access_token() -> Option<String> {
    std::env::var_os("ARES_TOKEN")
        .map(PathBuf::from)
        .or_else(|| Some(state_dir().join("access.token")))
        .and_then(|path| std::fs::read_to_string(path).ok())
        .map(|token| token.trim().to_owned())
        .filter(|token| !token.is_empty())
}

fn fetch_state(token: &str) -> Result<Value, String> {
    let base = std::env::var("ARES_ENGINE").unwrap_or_else(|_| DEFAULT_ENGINE.to_owned());
    let response = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|error| error.to_string())?
        .get(format!("{base}/v1/state"))
        .bearer_auth(token)
        .send()
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(format!("engine returned {}", response.status()));
    }
    response.json::<Value>().map_err(|error| error.to_string())
}

fn spawn_argv(session: Option<&Value>) -> Vec<String> {
    let mut argv = vec!["hermes".to_owned()];
    if let Some(session) = session {
        argv.push("-p".to_owned());
        argv.push(session["botId"].as_str().unwrap_or("default").to_owned());
        if let Some(stored) = session["storedSessionId"]
            .as_str()
            .filter(|s| !s.is_empty())
        {
            argv.push("--resume".to_owned());
            argv.push(stored.to_owned());
        }
    }
    argv
}

fn child_env() -> Vec<String> {
    let mut env: Vec<String> = std::env::vars()
        .map(|(key, value)| format!("{key}={value}"))
        .collect();
    env.retain(|entry| {
        !entry.starts_with("TERM=")
            && !entry.starts_with("COLORTERM=")
            && !entry.starts_with("HERDR_")
    });
    env.push("TERM=xterm-256color".to_owned());
    env.push("COLORTERM=truecolor".to_owned());
    env
}

fn text_of(value: &Value, key: &str) -> String {
    value[key].as_str().unwrap_or("").to_owned()
}

fn main() -> glib::ExitCode {
    let application = gtk4::Application::new(Some(APP_ID), gtk4::gio::ApplicationFlags::default());
    application.connect_activate(build_ui);
    application.run()
}

fn build_ui(app: &gtk4::Application) {
    let window = gtk4::ApplicationWindow::new(app);
    window.set_title(Some("Ares"));
    window.set_default_size(1400, 900);

    let content = gtk4::Box::new(gtk4::Orientation::Vertical, 0);
    let header = gtk4::HeaderBar::new();
    header.set_title_widget(Some(&gtk4::Label::new(Some("Ares"))));
    content.append(&header);

    let paned = gtk4::Paned::new(gtk4::Orientation::Horizontal);
    paned.set_position(280);
    paned.set_wide_handle(true);
    content.append(&paned);

    // Left: rooms.
    let room_list = gtk4::ListBox::new();
    room_list.set_selection_mode(gtk4::SelectionMode::Single);
    room_list.add_css_class("navigation-sidebar");
    let room_scroll = gtk4::ScrolledWindow::new();
    room_scroll.set_child(Some(&room_list));
    room_scroll.set_vexpand(true);
    paned.set_start_child(Some(&room_scroll));

    // Right: terminal + native artifact strip.
    let right = gtk4::Box::new(gtk4::Orientation::Vertical, 6);
    right.set_margin_start(6);
    right.set_margin_end(6);
    right.set_margin_bottom(6);

    let terminal = vte4::Terminal::new();
    terminal.set_hexpand(true);
    terminal.set_vexpand(true);
    let terminal_scroll = gtk4::ScrolledWindow::new();
    terminal_scroll.set_child(Some(&terminal));
    terminal_scroll.set_vexpand(true);
    right.append(&terminal_scroll);

    let strip_title = gtk4::Label::new(Some("Files"));
    strip_title.set_xalign(0.0);
    strip_title.add_css_class("heading");
    right.append(&strip_title);

    let strip = gtk4::FlowBox::new();
    strip.set_selection_mode(gtk4::SelectionMode::None);
    strip.set_max_children_per_line(12);
    let strip_scroll = gtk4::ScrolledWindow::new();
    strip_scroll.set_child(Some(&strip));
    strip_scroll.set_propagate_natural_height(true);
    strip_scroll.set_max_content_height(200);
    right.append(&strip_scroll);

    paned.set_end_child(Some(&right));
    window.set_child(Some(&content));

    // One lazy full-size preview window for the whole app.
    let preview = gtk4::Window::new();
    preview.set_title(Some("Ares — Preview"));
    preview.set_default_size(1100, 800);
    let preview_picture = gtk4::Picture::new();
    preview_picture.set_can_shrink(true);
    let preview_scroll = gtk4::ScrolledWindow::new();
    preview_scroll.set_child(Some(&preview_picture));
    preview.set_child(Some(&preview_scroll));

    // Room payloads keyed by room id (row widget name).
    let rooms: Rc<RefCell<HashMap<String, RoomData>>> = Rc::new(RefCell::new(HashMap::new()));

    // Load state from the engine, then fill the room list.
    let (tx, rx) = async_channel::bounded(1);
    std::thread::spawn(move || {
        let result = match access_token() {
            Some(token) => fetch_state(&token),
            None => Err("no access token (ARES_TOKEN or state dir)".to_owned()),
        };
        let _ = tx.send_blocking(result);
    });
    glib::spawn_future_local(glib::clone!(
        #[weak]
        room_list,
        #[strong]
        rooms,
        async move {
            let Ok(Ok(state)) = rx.recv().await else {
                eprintln!("ares-app: engine state unavailable");
                return;
            };
            let projects: HashMap<String, Value> = state["projects"]
                .as_array()
                .cloned()
                .unwrap_or_default()
                .into_iter()
                .map(|project| (text_of(&project, "id"), project))
                .collect();
            let artifacts = state["artifacts"].as_array().cloned().unwrap_or_default();
            for room in state["rooms"].as_array().cloned().unwrap_or_default() {
                let room_id = text_of(&room, "id");
                let alias = text_of(&room, "alias");
                let name = text_of(&room, "name");
                let label_text = if alias.is_empty() { name } else { alias };
                let project = projects.get(&text_of(&room, "projectId")).cloned();
                let subtitle = project
                    .as_ref()
                    .map(|project| text_of(project, "name"))
                    .unwrap_or_default();
                let cwd = project
                    .as_ref()
                    .map(|project| text_of(project, "path"))
                    .filter(|path| !path.starts_with("hermes://"))
                    .filter(|path| PathBuf::from(path).is_dir());

                let session = room["sessions"]
                    .as_array()
                    .and_then(|sessions| sessions.first())
                    .cloned();
                let room_artifacts: Vec<Value> = artifacts
                    .iter()
                    .filter(|artifact| text_of(artifact, "roomId") == room_id)
                    .filter(|artifact| {
                        let mime = text_of(artifact, "mime");
                        mime.starts_with("image/") || mime.starts_with("video/")
                    })
                    .filter(|artifact| PathBuf::from(text_of(artifact, "path")).exists())
                    .cloned()
                    .collect();
                rooms
                    .borrow_mut()
                    .insert(room_id.clone(), (session, room_artifacts, cwd));

                let row = gtk4::ListBoxRow::new();
                let box_row = gtk4::Box::new(gtk4::Orientation::Vertical, 2);
                box_row.set_margin_top(6);
                box_row.set_margin_bottom(6);
                box_row.set_margin_start(8);
                box_row.set_margin_end(8);
                let label = gtk4::Label::new(Some(&label_text));
                label.set_xalign(0.0);
                label.set_ellipsize(gtk4::pango::EllipsizeMode::End);
                box_row.append(&label);
                if !subtitle.is_empty() {
                    let sub = gtk4::Label::new(Some(&subtitle));
                    sub.set_xalign(0.0);
                    sub.add_css_class("dim-label");
                    sub.set_ellipsize(gtk4::pango::EllipsizeMode::End);
                    box_row.append(&sub);
                }
                row.set_child(Some(&box_row));
                row.set_widget_name(&room_id);
                room_list.append(&row);
            }
        }
    ));

    // Room selection: spawn the Hermes TUI in the terminal + fill the strip.
    room_list.connect_row_selected(glib::clone!(
        #[weak]
        terminal,
        #[weak]
        strip,
        #[strong]
        rooms,
        #[strong]
        preview_picture,
        move |_, row| {
            let Some(row) = row else { return };
            let room_id = row.widget_name().to_string();
            let Some((session, room_artifacts, cwd)) = rooms.borrow().get(&room_id).cloned() else {
                return;
            };

            while let Some(child) = strip.first_child() {
                strip.remove(&child);
            }
            for artifact in &room_artifacts {
                let path = text_of(artifact, "path");
                let name = text_of(artifact, "name");
                let mime = text_of(artifact, "mime");
                let widget: gtk4::Widget = if mime.starts_with("image/") {
                    let picture = gtk4::Picture::for_filename(&path);
                    picture.set_content_fit(gtk4::ContentFit::Cover);
                    picture.set_size_request(140, 140);
                    picture.set_tooltip_text(Some(&name));
                    let click = gtk4::GestureClick::new();
                    let preview_for_click = preview_picture.clone();
                    let path_for_click = path.clone();
                    click.connect_released(move |_, _, _, _| {
                        preview_for_click.set_filename(Some(&path_for_click));
                    });
                    picture.add_controller(click);
                    picture.upcast()
                } else {
                    let video = gtk4::Video::new();
                    video.set_size_request(140, 140);
                    video.set_tooltip_text(Some(&name));
                    let media = gtk4::MediaFile::for_filename(&path);
                    video.set_media_stream(Some(&media));
                    video.upcast()
                };
                strip.insert(&widget, -1);
            }
            terminal.reset(true, true);
            let argv: Vec<String> = spawn_argv(session.as_ref());
            let env = child_env();
            let argv: Vec<&str> = argv.iter().map(String::as_str).collect();
            let envv: Vec<&str> = env.iter().map(String::as_str).collect();
            let cwd = cwd;
            terminal.spawn_async(
                vte4::PtyFlags::DEFAULT,
                cwd.as_deref(),
                &argv,
                &envv,
                glib::SpawnFlags::SEARCH_PATH_FROM_ENVP,
                || {},
                -1,
                None::<&gtk4::gio::Cancellable>,
                move |result| {
                    if let Err(error) = result {
                        eprintln!("ares-app: TUI spawn failed: {error}");
                    }
                },
            );
        }
    ));

    window.present();
}
