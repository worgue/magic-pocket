from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from comments.models import Comment

PREFIX = "[sqs-worker]"
KEEP = 3


class Command(BaseCommand):
    """SQS worker 経由でハートビートコメントを 1 件投稿する。

    scheduler (pocket.sqs_scheduler) → SQS queue → sqsmanagement handler →
    本 command、という非同期経路の動作確認に使う。古い自動投稿は最新 KEEP 件を
    残して削除するので、定期実行しても溜まらない。
    """

    help = "Post a heartbeat comment via the SQS worker (async path probe)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--fail",
            action="store_true",
            help="意図的に失敗する (retry / DLQ の動作確認用)",
        )

    def handle(self, *args, **options):
        if options["fail"]:
            raise CommandError("intentional failure for DLQ verification")
        user = User.objects.filter(is_superuser=True).order_by("pk").first()
        if user is None:
            raise CommandError("superuser not found (run create_admin first)")
        now = timezone.now().isoformat(timespec="seconds")
        comment = Comment.objects.create(user=user, text=f"{PREFIX} heartbeat at {now}")
        old_pks = [
            c.pk
            for c in Comment.objects.filter(
                user=user, text__startswith=PREFIX
            ).order_by("-created_at")[KEEP:]
        ]
        if old_pks:
            Comment.objects.filter(pk__in=old_pks).delete()
        self.stdout.write(f"comment {comment.pk} created (pruned {len(old_pks)})")
