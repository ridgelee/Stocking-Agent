import uuid
from django.db import models


class NewsArticle(models.Model):
    # 基础字段（采集阶段填充）
    article_id    = models.UUIDField(default=uuid.uuid4, unique=True)
    source_type   = models.CharField(max_length=20)   # tech_media | financial_news | social_media
    source_name   = models.CharField(max_length=100)
    title         = models.TextField()
    content       = models.TextField()
    url           = models.URLField(unique=True, max_length=2048)
    published_at  = models.DateTimeField()
    fetched_at    = models.DateTimeField(auto_now_add=True)

    # Claude 提取字段（提取阶段填充）
    related_tickers  = models.JSONField(default=list)
    event_type       = models.CharField(max_length=30, null=True, blank=True)
    # earnings | merger_acquisition | policy | product_launch | market_movement | other
    sentiment        = models.CharField(max_length=10, null=True, blank=True)
    # Bullish | Bearish | Neutral
    impact_level     = models.CharField(max_length=10, null=True, blank=True)
    # High | Medium | Low
    one_line_summary = models.TextField(null=True, blank=True)
    key_entities     = models.JSONField(default=list)
    is_extracted     = models.BooleanField(default=False)

    class Meta:
        ordering = ['-published_at']
        indexes = [
            models.Index(fields=['published_at']),
            models.Index(fields=['impact_level']),
            models.Index(fields=['sentiment']),
            models.Index(fields=['source_type']),
            models.Index(fields=['is_extracted']),
        ]

    def __str__(self):
        return f"[{self.source_type}] {self.title[:60]}"


class DailyReport(models.Model):
    report_date  = models.DateField(unique=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    report_data  = models.JSONField()
    status       = models.CharField(max_length=20, default='completed')
    # completed | failed | generating

    class Meta:
        ordering = ['-report_date']

    def __str__(self):
        return f"DailyReport {self.report_date} [{self.status}]"
