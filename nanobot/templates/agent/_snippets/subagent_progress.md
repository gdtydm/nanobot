{% if commentary %}
Before your first tool call, and at meaningful phase changes when calling tools, write one or two brief sentences as ordinary, user-visible assistant text in the same response as the tool call so `/task` records a live timeline. Start the commentary with `[Milestone]` only to notify the originating chat of a major verified stage completion or a blocking failure that changes the plan.
{% else %}
At the start of the task and at meaningful phase changes (such as moving from investigation to edits or execution, submitting or monitoring a long-running job, diagnosing a failure, or starting verification), call `report_progress` with `notify=false` alongside your next tool call in the same turn so `/task` records a live timeline. Set `notify=true` only to notify the originating chat of a major verified stage completion or a blocking failure that changes the plan.
{% endif %}

Group related results into one update and include a verified run ID, artifact, or next step when relevant. Keep routine setup, individual file edits, job submission or waiting/polling, intermediate log inspection, and pre-final wrap-up messages out of chat notifications — record them silently for `/task` instead.
