from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .models import Comment


def comment_list(request):
    comments = Comment.objects.select_related("user").all()[:50]
    return render(request, "comments/list.html", {"comments": comments})


@login_required
def comment_create(request):
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        if text:
            Comment.objects.create(user=request.user, text=text)
        return redirect("comment_list")
    return render(request, "comments/create.html")
