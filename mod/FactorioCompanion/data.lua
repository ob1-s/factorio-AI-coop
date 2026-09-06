local inputs = {
  {
    type = "custom-input",
    name = "companion-toggle-chat",
    key_sequence = "CONTROL + SHIFT + SPACE",
    consuming = "none"
  },
  {
    type = "custom-input",
    name = "companion-toggle-tasks",
    key_sequence = "CONTROL + SHIFT + T",
    consuming = "none"
  }
}

data:extend(inputs)

require("styles")
