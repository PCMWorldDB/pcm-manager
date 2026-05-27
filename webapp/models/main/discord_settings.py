"""Main database model - namespace-scoped Discord settings."""
from webapp.extensions import db
from webapp.models.main.base import MainDBModel


class DiscordSettings(MainDBModel):
    """Discord notification workflow target for one namespace."""

    __tablename__ = 'discord_settings'

    WORKFLOW_STATS_EDITOR_APPROVALS = 'stats_editor_approvals'
    WORKFLOW_RELEASE_APPROVALS = 'release_approvals'

    namespace_id = db.Column(db.Integer, db.ForeignKey('namespaces.id'), nullable=False, index=True)
    name = db.Column(db.String(120))
    workflow = db.Column(db.String(80), nullable=False, default=WORKFLOW_STATS_EDITOR_APPROVALS, index=True)
    is_enabled = db.Column(db.Boolean, nullable=False, default=False)
    channel_id = db.Column(db.String(64))
    approver_role_id = db.Column(db.String(64))
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_by_id = db.Column(db.Integer, db.ForeignKey('users.id'))

    namespace = db.relationship('Namespace')
    created_by = db.relationship('User', foreign_keys=[created_by_id])
    updated_by = db.relationship('User', foreign_keys=[updated_by_id])


class DiscordWorkflowThread(MainDBModel):
    """Discord thread created for a workflow target and stat edit scope."""

    __tablename__ = 'discord_workflow_threads'

    namespace_id = db.Column(db.Integer, db.ForeignKey('namespaces.id'), nullable=False, index=True)
    discord_settings_id = db.Column(db.Integer, db.ForeignKey('discord_settings.id', ondelete='CASCADE'), nullable=False, index=True)
    workflow = db.Column(db.String(80), nullable=False, index=True)
    first_cycling_race_id = db.Column(db.Integer, nullable=True, index=True)
    first_cycling_stage_id = db.Column(db.Integer, nullable=True, index=True)
    stat_list_id = db.Column(db.Integer, nullable=True, index=True)
    thread_id = db.Column(db.String(64), nullable=False)
    thread_url = db.Column(db.String(255))

    namespace = db.relationship('Namespace')
    discord_settings = db.relationship('DiscordSettings')


DISCORD_WORKFLOW_CHOICES = (
    (DiscordSettings.WORKFLOW_STATS_EDITOR_APPROVALS, 'Stats Editor Approvals'),
    (DiscordSettings.WORKFLOW_RELEASE_APPROVALS, 'Release Approvals'),
)
