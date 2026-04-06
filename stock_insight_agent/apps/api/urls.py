from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('api/news/', views.news_list, name='news_list'),
    path('api/portfolio/', views.portfolio, name='portfolio'),
    path('api/report/generate/', views.report_generate, name='report_generate'),
    path('api/report/latest/', views.report_latest, name='report_latest'),
    path('api/chat/', views.chat, name='chat'),
]
