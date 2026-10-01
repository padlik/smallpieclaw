| Term | Definition | Use When | Avoid |
| --- | --- | --- | --- |
| Operator turn | One interactive request and its owned work, ending after all on-demand children are accounted for. | Describing interactive lifecycle and approval ownership. | Treating a scheduled execution as part of the turn. |
| Turn-owned child | An on-demand sub-agent whose lifecycle and result belong to its spawning operator turn. | Distinguishing parent-consumed work from autonomous jobs. | Calling it a detached job. |
| Scheduled run | One execution of a stored schedule, including a manual `run_now` trigger, with job-owned context and result. | Describing scheduler-owned execution policy. | Treating `run_now` as an interactive on-demand child. |
| Job-owned context | Input and optional persisted conversation belonging to one scheduled job, without operator memory or recall. | Describing scheduled-run isolation. | Implying that shared permitted files are isolated. |
| Job execution record | The bounded-history status and full output of a scheduled run, independent of its Telegram presentation. | Describing result retrieval and notification. | Treating the notification as the durable result store. |
