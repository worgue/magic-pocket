from django.urls import path

from . import views

urlpatterns = [
    path("", views.comment_list, name="comment_list"),
    path("create/", views.comment_create, name="comment_create"),
]
