data:extend({
  {
    type = "bool-setting",
    name = "companion-notifications",
    setting_type = "runtime-per-user",
    default_value = true,
    order = "a"
  },
  {
    type = "int-setting",
    name = "companion-chat-history-limit",
    setting_type = "runtime-per-user",
    default_value = 200,
    minimum_value = 20,
    maximum_value = 1000,
    order = "b"
  }
})
