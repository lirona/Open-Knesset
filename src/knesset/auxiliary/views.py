import random
from operator import attrgetter
from django.template import RequestContext
from django.shortcuts import render_to_response, get_object_or_404
from django.utils.translation import ugettext as _
from django.conf import settings
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.http import HttpResponseForbidden, HttpResponseRedirect, \
    HttpResponse, HttpResponseNotAllowed, HttpResponseBadRequest
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.contenttypes.models import ContentType
import tagging
from actstream import action
from knesset.mks.models import Member
from knesset.laws.models import Vote,Bill
from knesset.committees.models import Topic, CommitteeMeeting
from tagging.models import Tag, TaggedItem
from annotatetext.views import post_annotation as annotatetext_post_annotation
from annotatetext.models import Annotation
from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView
from django.views.generic.list import ListView
from django.contrib.comments.models import Comment
from knesset.utils import notify_responsible_adult

import logging
logger = logging.getLogger("open-knesset.auxiliary.views")

def help_page(request):
    context = cache.get('help_page_context')
    if not context:
        context = {}
        context['title'] = _('Help')
        context['member'] = Member.objects.all()[random.randrange(Member.objects.count())]
        votes = Vote.objects.filter_and_order(order='controversy')
        context['vote'] = votes[random.randrange(votes.count())]
        context['bill'] = Bill.objects.all()[random.randrange(Bill.objects.count())]
        tags = Tag.objects.cloud_for_model(Bill)
        context['tags'] = random.sample(tags, min(len(tags),8)) if tags else None
        context['has_search'] = False # enable the base template search
        cache.set('help_page_context', context, 300) # 5 Minutes
    template_name = '%s.%s%s' % ('help_page', settings.LANGUAGE_CODE, '.html')
    return render_to_response(template_name, context, context_instance=RequestContext(request))

def main(request):
    context = cache.get('main_page_context')
    if not context:
        context = {}
        context['title'] = _('Home')
        annotations = \
            list(Annotation.objects.all().order_by('-timestamp')[:10])
        for a in annotations:
            a.submit_date = a.timestamp
        comments = list(Comment.objects.all().order_by('-submit_date')[:10])
        previous_comments = set()
        for c in comments:
            c.previous_comments = Comment.objects.filter(object_pk=c.object_pk,
                                                         content_type=c.content_type,
                                                         submit_date__lt=c.submit_date)
            previous_comments.update(c.previous_comments)
            c.is_comment = True
        comments = [c for c in comments if c not in previous_comments]
        annotations.extend(comments)
        annotations.sort(key=lambda x:x.submit_date,reverse=True)
        context['annotations'] = annotations
        context['topics'] = Topic.objects.summary('-modified')[:20]
        context['has_search'] = True # disable the base template search
        cache.set('main_page_context', context, 300) # 5 Minutes
    template_name = '%s.%s%s' % ('main', settings.LANGUAGE_CODE, '.html')
    return render_to_response(template_name, context, context_instance=RequestContext(request))

def post_annotation(request):
    if request.user.has_perm('annotatetext.add_annotation'):
        return annotatetext_post_annotation(request)
    else:
        return HttpResponseForbidden(_("Sorry, you do not have the permission to annotate."))

def search(request, lang='he'):

    # remove the 'cof' get variable from the query string so that the page
    # linked to by the javascript fallback doesn't think its inside an iframe.
    mutable_get = request.GET.copy()
    if 'cof' in mutable_get:
        del mutable_get['cof']

    return render_to_response('search/search.html', RequestContext(request, {
        'query': request.GET.get('q'),
        'query_string': mutable_get.urlencode(),
        'has_search': True,
        'lang' : lang,
        'cx': request.GET.get('cx')
    }))

def post_details(request, post_id):
    ''' patching django-planet's post_detail view so it would update the
        hitcount and redirect to the post's url
    '''
    from hitcount.views import _update_hit_count
    from hitcount.models import HitCount
    from planet.models import Post

    # update the it count
    ctype = ContentType.objects.get(app_label="planet", model="post")
    hitcount, created = HitCount.objects.get_or_create(content_type=ctype,
                                                  object_pk=post_id)
    result = _update_hit_count(request, hitcount)
    post = get_object_or_404(Post, pk=post_id)
    return HttpResponseRedirect(post.url)


class RobotsView(TemplateView):
    """Return the robots.txt"""

    template_name = 'robots.txt'

    def render_to_response(self, context, **kwargs):
        return super(RobotsView, self).render_to_response(context,
                        content_type='text/plain', **kwargs)


class AboutView(TemplateView):
    """About template"""

    template_name = 'about.html'


class CommentsView(ListView):
    """Comments index view"""

    model = Comment
    queryset = Comment.objects.order_by("-submit_date")

    paginate_by = 20

def _add_tag_to_object(user, app, object_type, object_id, tag):
    ctype = ContentType.objects.get_by_natural_key(app, object_type)
    (ti, created) = TaggedItem._default_manager.get_or_create(tag=tag, content_type=ctype, object_id=object_id)
    action.send(user,verb='tagged', target=ti, description='%s' % (tag.name))
    url = reverse('tag-detail', kwargs={'slug':tag.name})
    return HttpResponse("{'id':%d,'name':'%s', 'url':'%s'}" % (tag.id,tag.name,url))

@login_required
def add_tag_to_object(request, app, object_type, object_id):
    """add a POSTed tag_id to object_type object_id by the current user"""
    if request.method == 'POST' and 'tag_id' in request.POST: # If the form has been submitted...
        tag = get_object_or_404(Tag,pk=request.POST['tag_id'])
        return _add_tag_to_object(request.user, app, object_type, object_id, tag)

    return HttpResponseNotAllowed(['POST'])

@login_required
def remove_tag_from_object(request, app, object_type, object_id):
    """remove a POSTed tag_id from object_type object_id"""
    ctype = ContentType.objects.get_by_natural_key(app, object_type)
    if request.method == 'POST' and 'tag_id' in request.POST: # If the form has been submitted...
        tag = get_object_or_404(Tag,pk=request.POST['tag_id'])
        ti = TaggedItem._default_manager.filter(tag=tag, content_type=ctype, object_id=object_id)
        if len(ti)==1:
            logger.debug('user %s is deleting tagged item %d' % (request.user.username, ti[0].id))
            ti[0].delete()
            action.send(request.user,verb='removed-tag', target=ti[0], description='%s' % (tag.name))
        else:
            logger.debug('user %s tried removing tag %d from object, but failed, because len(tagged_items)!=1' % (request.user.username, tag.id))
    return HttpResponse("{'id':%d,'name':'%s'}" % (tag.id,tag.name))

@permission_required('tagging.add_tag')
def create_tag_and_add_to_item(request, app, object_type, object_id):
    """adds tag with name=request.POST['tag'] to the tag list, and tags the given object with it"""
    if request.method == 'POST' and 'tag' in request.POST:
        tag = request.POST['tag']
        msg = "user %s is creating tag %s on object_type %s and object_id %s".encode('utf8') % (request.user.username, tag, object_type, object_id)
        logger.info(msg)
        notify_responsible_adult(msg)
        if len(tag)<3:
            return HttpResponseBadRequest()
        tags = Tag.objects.filter(name=tag)
        if not tags:
            try:
                tag = Tag.objects.create(name=tag)
            except Exception:
                logger.warn("can't create tag %s" % tag)
                return HttpResponseBadRequest()
        if len(tags)==1:
            tag = tags[0]
        if len(tags)>1:
            logger.warn("More than 1 tag: %s" % tag)
            return HttpResponseBadRequest()
        return _add_tag_to_object(request.user, app, object_type, object_id, tag)
    else:
        return HttpResponseNotAllowed(['POST'])


def calculate_cloud_from_models(*args):
    from tagging.models import Tag 
    cloud = Tag._default_manager.cloud_for_model(args[0])
    for model in args[1:]:
        for tag in Tag._default_manager.cloud_for_model(model):
            if tag in cloud:
                cloud[cloud.index(tag)].count+=tag.count
            else:
                cloud.append(tag)
    return tagging.utils.calculate_cloud(cloud)

class TagList(ListView):
    """Tags index view"""

    model = Tag
    template_name = 'auxiliary/tag_list.html'

    def get_queryset(self):
        return Tag.objects.all()

    def get_context_data(self, **kwargs):
        context = super(TagList, self).get_context_data(**kwargs)
        tags_cloud = calculate_cloud_from_models(Vote,Bill,CommitteeMeeting)
        context['tags_cloud'] = tags_cloud
        return context

class TagDetail(DetailView):
    """Tags index view"""

    model = Tag
    template_name = 'auxiliary/tag_detail.html'
    slug_field = 'name'
    def get_context_data(self, **kwargs):
        context = super(TagDetail, self).get_context_data(**kwargs)
        tag = context['object']
        bills_ct = ContentType.objects.get_for_model(Bill)
        bills = [ti.object for ti in
                    TaggedItem.objects.filter(tag=tag, content_type=bills_ct)]
        context['bills'] = bills
        votes_ct = ContentType.objects.get_for_model(Vote)
        votes = [ti.object for ti in
                    TaggedItem.objects.filter(tag=tag, content_type=votes_ct)]
        context['votes'] = votes
        cm_ct = ContentType.objects.get_for_model(CommitteeMeeting)
        cms = [ti.object for ti in
                    TaggedItem.objects.filter(tag=tag, content_type=cm_ct)]
        context['cms'] = cms
        return context


