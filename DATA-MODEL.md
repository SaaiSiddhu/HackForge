# Data model

Events own tracks, teams, judges and projects. Projects reference their team and track. Reviews use a compound judge/project key. Fixture string identifiers and UTC timestamps are retained; duplicate project records are not silently removed.

The foundation stores imported team member lists, judge track lists and score criteria as JSON. Subsequent migrations will introduce account-linked memberships, track permissions, rubric criteria and assignments before any authorization or score editing relies on these records. These JSON fields are import storage, not an implemented access-control system.
