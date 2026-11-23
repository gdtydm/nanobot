{% if commentary %}
Use brief commentary when there is progress to record. Start the commentary with `[Milestone]` only to notify the originating chat of a verified result that materially advances a multi-step task.
{% else %}
Use `report_progress` to record a meaningful development in `/task`. Set `notify=true` only to notify the originating chat of a verified result that materially advances a multi-step task.
{% endif %}

Group related results into one update. Mention what changed and include a verified run ID, artifact, or next step when relevant. Examples are a published artifact, an accepted external run, and a completed validation. Keep worktree setup, individual file edits, tool activity, and waiting out of chat updates.
