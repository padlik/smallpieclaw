## [Unreleased]

### Changed
- **Native tool calling**: when a model returns plain text (no tool_calls) in the native path, the run ends immediately. In json_mode, the same response would trigger a re-prompt. This is correct per the native protocol but is a behavioral change — if a model prose-responds mid-run, check logs for the WARNING above.
