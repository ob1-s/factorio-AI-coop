local styles = data.raw["gui-style"]["default"]

styles["companion_frame"] = {
  type = "frame_style",
  parent = "inside_shallow_frame_with_padding",
  top_padding = 6,
  right_padding = 8,
  bottom_padding = 8,
  left_padding = 8
}

styles["companion_header_spacer"] = {
  type = "empty_widget_style",
  minimal_width = 4,
  horizontally_stretchable = "on"
}

styles["companion_scroll"] = {
  type = "scroll_pane_style",
  parent = "scroll_pane",
  vertical_padding = 4,
  horizontal_padding = 2
}

styles["companion_chat_scroll"] = {
  type = "scroll_pane_style",
  parent = "scroll_pane",
  vertical_padding = 4,
  horizontal_padding = 2
}

styles["companion_chat_bubble"] = {
  type = "frame_style",
  parent = "inside_shallow_frame_with_padding",
  left_padding = 8,
  right_padding = 8,
  top_padding = 5,
  bottom_padding = 5,
  vertically_stretchable = "off"
}

styles["companion_chat_system"] = {
  type = "frame_style",
  parent = "companion_chat_bubble"
}

styles["companion_chat_text"] = {
  type = "label_style",
  parent = "label",
  font = "default"
}

styles["companion_chat_meta"] = {
  type = "label_style",
  parent = "label",
  font = "default-semibold",
  font_color = { 0.55, 0.85, 0.55 },
  minimal_height = 16
}

styles["companion_chat_meta_user"] = {
  type = "label_style",
  parent = "label",
  font = "default-semibold",
  font_color = { 0.65, 0.78, 0.95 },
  minimal_height = 16
}

styles["companion_status_label"] = {
  type = "label_style",
  parent = "label",
  font = "default-semibold",
  font_color = { 0.95, 0.75, 0.3 },
  left_margin = 6
}

styles["companion_input"] = {
  type = "textbox_style",
  parent = "textbox",
  minimal_width = 200
}

styles["companion_send_button"] = {
  type = "button_style",
  parent = "tool_button",
  width = 28,
  height = 28
}

styles["companion_logo_button"] = {
  type = "button_style",
  parent = "tool_button",
  width = 24,
  height = 24
}

styles["companion_pill_button"] = {
  type = "button_style",
  parent = "button",
  width = 34,
  height = 34,
  font = "default-bold"
}

styles["companion_task_row"] = {
  type = "frame_style",
  parent = "inside_shallow_frame_with_padding",
  left_padding = 6,
  right_padding = 6,
  top_padding = 4,
  bottom_padding = 4,
  bottom_margin = 3,
  vertically_stretchable = "off"
}

styles["companion_task_status_button"] = {
  type = "button_style",
  parent = "tool_button",
  width = 22,
  height = 22,
  right_margin = 6
}

styles["companion_task_text"] = {
  type = "label_style",
  parent = "label",
  font = "default-semibold"
}

styles["companion_task_detail"] = {
  type = "label_style",
  parent = "label",
  font_color = { 0.72, 0.72, 0.72 }
}

styles["companion_info_body"] = {
  type = "label_style",
  parent = "label",
  bottom_margin = 6
}

styles["companion_table"] = {
  type = "table_style",
  parent = "table",
  horizontal_spacing = 10,
  vertical_spacing = 3
}

styles["companion_table_key"] = {
  type = "label_style",
  parent = "label",
  font = "default-semibold",
  font_color = { 0.7, 0.8, 0.9 }
}

styles["companion_table_value"] = {
  type = "label_style",
  parent = "label"
}

styles["companion_chat_user"] = {
  type = "frame_style",
  parent = "companion_chat_bubble"
}

styles["companion_chat_agent"] = {
  type = "frame_style",
  parent = "companion_chat_bubble"
}
