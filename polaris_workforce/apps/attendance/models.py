"""Attendance days, records and corrections."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class AttendanceDay(TimeStampedModel):
    """One site's roll call for one date: Draft → Submitted → Approved / Rejected → Locked."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SUBMITTED = "submitted", _("Submitted")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        LOCKED = "locked", _("Locked")

    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="attendance_days",
        db_index=False,  # unique_attendance_day_per_site starts with it
    )
    date = models.DateField(_("date"))
    foreman = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("foreman"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="attendance_days",
    )
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    submitted_at = models.DateTimeField(_("submitted at"), null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("submitted by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_attendance_days",
    )
    client_submitted_at = models.DateTimeField(
        _("submitted on the phone at"),
        null=True,
        blank=True,
        help_text=_("When the foreman tapped Submit, if the phone said so (it may sync later)."),
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("approved by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_attendance_days",
    )
    rejection_note = models.TextField(_("rejection note"), blank=True)
    foreman_note = models.TextField(_("foreman note"), blank=True)
    client_uuid = models.UUIDField(_("client UUID"), unique=True, null=True, blank=True)

    class Meta:
        verbose_name = _("attendance day")
        verbose_name_plural = _("attendance days")
        ordering = ["-date", "site"]
        constraints = [
            models.UniqueConstraint(fields=["site", "date"], name="unique_attendance_day_per_site"),
        ]
        indexes = [
            models.Index(fields=["status", "date"]),
            models.Index(fields=["updated_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.date}"

    @property
    def is_locked(self) -> bool:
        return self.status == self.Status.LOCKED


class AttendanceRecord(TimeStampedModel):
    """One worker's status on an attendance day."""

    class Status(models.TextChoices):
        PRESENT = "present", _("Present")
        ABSENT = "absent", _("Absent")
        HALF_DAY = "half_day", _("Half day")
        LATE = "late", _("Late")

    day = models.ForeignKey(
        AttendanceDay,
        verbose_name=_("attendance day"),
        on_delete=models.CASCADE,
        related_name="records",
        db_index=False,  # unique_record_per_worker_per_day starts with it
    )
    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.PROTECT,
        related_name="attendance_records",
    )
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.PRESENT
    )
    arrival = models.TimeField(_("arrival"), null=True, blank=True)
    departure = models.TimeField(_("departure"), null=True, blank=True)
    note = models.TextField(_("note"), blank=True)
    client_uuid = models.UUIDField(_("client UUID"), unique=True, default=uuid.uuid4)
    corrected = models.BooleanField(
        _("corrected"), default=False, help_text=_("Changed by a correction after submission.")
    )
    recorded_at = models.DateTimeField(_("recorded at"), default=timezone.now)

    class Meta:
        verbose_name = _("attendance record")
        verbose_name_plural = _("attendance records")
        ordering = ["day", "worker"]
        constraints = [
            models.UniqueConstraint(
                fields=["day", "worker"], name="unique_record_per_worker_per_day"
            ),
        ]
        indexes = [models.Index(fields=["updated_at"])]

    def __str__(self) -> str:
        return f"{self.worker} · {self.day.date} · {self.get_status_display()}"


class AttendanceCorrection(models.Model):
    """A change to a record after submission. The reason is mandatory."""

    record = models.ForeignKey(
        AttendanceRecord,
        verbose_name=_("record"),
        on_delete=models.CASCADE,
        related_name="corrections",
    )
    field = models.CharField(_("field"), max_length=64)
    old_value = models.TextField(_("old value"), blank=True)
    new_value = models.TextField(_("new value"), blank=True)
    reason = models.TextField(_("reason"))
    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("corrected by"),
        on_delete=models.PROTECT,
        related_name="attendance_corrections",
    )
    client_uuid = models.UUIDField(_("client UUID"), unique=True, null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("attendance correction")
        verbose_name_plural = _("attendance corrections")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["created_at"])]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(reason=""), name="correction_reason_required"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.record} · {self.field}: {self.old_value} → {self.new_value}"

    def save(self, *args, **kwargs):
        # A reason made only of spaces is no reason: strip it so the check rejects it.
        self.reason = (self.reason or "").strip()
        super().save(*args, **kwargs)
