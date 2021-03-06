#!/usr/bin/python
from __future__ import division

from twisted.web.server import Site, NOT_DONE_YET
from twisted.internet import reactor
from twisted.web.resource import Resource, IResource, NoResource
from twisted.internet.defer import inlineCallbacks, returnValue, Deferred
from twisted.web.template import flattenString, renderer
from labrad.server import LabradServer, setting
import functools
import inspect
import labrad
from zope.interface import implements

"""
### BEGIN NODE INFO
[info]
name = HTTP Server
version = 1.0
description = Cryo status information over HTTP

[startup]
cmdline = %PYTHON% %FILE%
timeout = 20

[shutdown]
message = 987654321
timeout = 5
### END NODE INFO
"""

def render_safe(render_method):
    '''
    This decorator wraps a rendering function from a twisted web
    template with a version that just returns an error message if an
    exception is raised.  The idea is that if you have a web page that
    requests status from a bunch of labrad servers but one is broken
    or not started, the rest of the page will render correctly.

    We can handle something that returns a value or returns a deferred.
    '''
    def wrapper(obj, request, tag):
        def error_message(err):
            tag.clear()
            return tag("Unable to get content for replacement: %s, exception: %s" % ( render_method.__name__, err))

        try:
            rv = render_method(obj, request, tag)
            if isinstance(rv, Deferred):
                rv.addErrback(error_message)
            return rv
        except Exception as e:
            return error_message(e)

    functools.update_wrapper(wrapper, render_method)
    if wrapper.__doc__:
        wrapper.__doc__ += "\nWrapped by render_safe"
    else:
        wrapper.__doc__ = "Wrapped by render_safe"
    return renderer(wrapper)

class RootStatusResource(Resource):
    '''
    The idea here is to have multiple status pages, each with their
    own Element subclass and .xml template, and have them automatically
    registered as subdirectories.  We would then provide a directory here
    browse.
    '''
    isLeaf=False
    def __init__(self, cxn=None):
        self.cxn = cxn
        Resource.__init__(self)
    def getChild(self, name, request):
        
        # Look to filesystem for an appropriately named module
        # Create a StatusPage with the Element from that module
        # and add it with putChild for direct lookup next time
        # 
        # if name=="" we should instead return a dictionary of all known modules
        try:
            page_factory = __import__("modules.%s"%name, globals=globals(), fromlist=['page_factory']).page_factory
            child = StatusResource(page_factory, self.cxn)
            self.putChild(name, child)
            print "successfully registered resource %s" % name
            return child
        except ImportError as e:
            return NoResource("No such child resource '%s'.  Error: %s" % (name, e))
    #def render_GET(self, request):
        # Generate a directory of known status pages
        #pass

class StatusResource(Resource):
    '''
    Generic class for a LabRAD based status page.  It takes a twisted template 'Element' subclass
    and the labrad client connection and uses them to generate a response page.  twisted templates
    can return deferreds, so they work well with LabRAD calls.
    '''
    isLeaf=True
    def __init__(self, page_factory, cxn=None):
        self.factory = page_factory
        self.cxn = cxn
    def _delayedRender(self, request, data):
        request.write(data)
        request.finish()
    def set_cxn(self, cxn):
        self.cxn = cxn
    def render_GET(self, request):
        if self.cxn is None:
            return "Unable to connect to labrad.  Sorry"
        d = flattenString(None, self.factory(self.cxn, request))
        d.addCallback(lambda data: self._delayedRender(request, data))
        return NOT_DONE_YET

class HTTPServer(LabradServer):
    """
    HTTP server to provide cryo status information

    Currently there are no exported labrad settings.  This is only a server to allow it to
    be easily started and stopped by the node.  
    """
    name = 'HTTP Server 2'

    def initServer(self):
        root = RootStatusResource(self.client)
        factory = Site(root)
        reactor.listenTCP(8881, factory)

__server__ = HTTPServer()

if __name__ == '__main__':
    from labrad import util
    util.runServer(__server__)
