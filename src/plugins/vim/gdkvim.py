# -*- coding: utf-8 -*- 
# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
# $Id: gdkvim.py 453 2005-07-24 15:24:10Z aafshar $
#Copyright (c) 2005 Ali Afshar aafshar@gmail.com

#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:

#The above copyright notice and this permission notice shall be included in
#all copies or substantial portions of the Software.

#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#SOFTWARE.


"""
A library to control vim -g using its X protocol interface (with gdk).
 
== How it works ==

=== General Communication ===

The Vim client/server protocol communicates by sending messages to and from an
X communication window. The details are explained in the Vim source.
Essentially, Vim understands two sorts of messages over this interface.

;asynchronous key sends : that are exactly equivalent to to the user of the
remote Vim typing commands.

;synchronous expression evaluations : these are Vim expressions that are
evaluated by the remote Vim, and an answer is replied with the result over the
same protocol.

Although the synchronous messages are called synchronous, the reply itself, in
programming terms is entirely asynchronous, in that there is no way of knowing
when a reply will be received, and one cannot block for it.

Thus, this library allows you to make both of these calls to remote Vims.
Synchronous expressions must provide a call back function that will be called
when the message is replied to.

=== The Server List ===

(It has been an utter nightmare.)

The primary problem is that GTK does not actually know accurately whether
a window with a given window ID has been destroyed. This is how Vim does
it (using the X libraries) after checking an attribute for registered Vim
sessions with the X root window. This way each Vim doesn't need to
unregister itself with the X root window on dying, it just assumes that
any other client attempting to connect to it will know that the window
has been destroyed. As mentioned, GTK totally fails to do what the X
library does, and ascertain whether the window is alive. It succeeds
sometimes, but not at others. The result is a GDK window that appears
alive, and ready to communicate with, but which causes an uncatchable and
fatal application error.

Step in other potential methods of getting an accurate list of servers.
Firstly, and most obviously, one can call the command 'vim --serverlist'
on a simple system pipe and read the list off. This is entirely reliable,
and effective, but the cost of forking a process and starting Vim each
time is not fun, and effectively blocks.

Another option is to force users to start Vim through Pida and keep an
account of the child processes. This would work very effectively, but it
restricts the user, and the entire system.

The final, and current solution is to start Vim itself on a
pseudoterminal as a hidden instance, and then communicate with that over
the Vim protocol. The reason this can be reliably done, is that since the
process is a child, it can be polled to check whether it is alive. This
is performed each time the serverlist is requested, and if the hidden
instance has been destroyed (eg by the user) a new one is spawned, thus
preventing an attempt to communicate with an already-destroyed GDK
window.

The cost of this solution is that we spawn an extra Vim process. I
believe that the added solidity it brings to the entire system is easily
worth it, and it ensures that Pida can communicate with Vim it started
and Vim it didn't start.
"""
# Gtk imports
import gtk
import gobject
import gtk.gdk as gdk
# System imports
import os
import pty
import sys
import time
import pida.base as base

class VimHidden(base.pidaobject):
    """
    An instance of Vim on a pseudoterminal which can be reliably polled.

    This class is used to provide an instance of Vim which can be communicated
    with using the Vim client/server protocol, in order to retrieve an accurate
    and current server list, and also which can be polled accurately as to
    whether it is alive before communicating with it.

    This method is much cheaper in resources than running vim --serverlist each
    time, and much more accurate than using the root window's VimRegistry
    property, and also more accurate than using GDK methods for assessing
    whether a window is alive.
    """

    def do_init(self):
        """
        Constructor.
        
        Create a temporary and unique name for use as the servername, and
        initialise the instance variables.

        @param cb: An instance of the main application class.
        @type cb: pida.main.Application.
        """
        # Prefacing with '__' means it will be ignored in the internal server
        # list.
        self.name = '__%s_PIDA_HIDDEN' % time.time()
        # Checked to evaluate False on starting.
        self.pid = None

    def start(self):
        """
        Start the Vim instance if it is not already running.
        
        This command forks in a pseudoterminal, and starts Vim, if Vim is not
        already running. The pid is stored for later use.
        """
        if not self.pid:
            # Get the console vim executable path
            command = self.prop_main_registry.commands.vim.value()
            # Fork using pty.fork to prevent Vim taking the terminal
            sock = gtk.Socket()
            w = gtk.Window()
            w.realize()
            w.add(sock)
            xid = sock.get_id()
            pid, fd = pty.fork()
            if pid == 0:
                # Child, execute Vim with the correct servername argument
                os.execvp(command, ['gvim', '-f', '--servername', self.name,
                    '--socketid', '%s' % xid])
                    #'-v'])
                # os.system('%s -v --servername %s' % (command, self.name))
            else:
                # Parent, store the pid, and file descriptor for later.
                self.pid = pid
                self.childfd = fd
                self.do_action('accountfork', self.pid)

    def is_alive(self):
        """
        Check if the Vim instance is alive.
        
        This method uses os.waitpid, with no blocking to determine whether the
        process is still alive. If it is not, it sets the internal pid
        attribute to None, so that it may be restarted.
        
        @returns: alive
        @rtype alive: boolean
        """
        if self.pid:
            try:
                # call os.waitpid, returns 0 if the pid is alive
                pid, sts = os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                # might still be starting up
                return False
            if pid == self.pid:
                # has shut down
                self.pid = None
                return False
            else:
                # is still alive
                return True
        else:
            # Not started yet
            return False

class VimWindow(base.pidaobject, gtk.Window):
    """
    A GTK window that can communicate with any number Vim instances.

    This is an actual GTK window (which it must be to accurately detect
    property events inside the GTK main loop) but has its GDK window correctly
    set to receive such events. This is notably the "Vim" property which must
    be present and set to a version string, in this case "6.0" is used.
    """
    def do_init(self):
        """
        Constructor.

        The Window is instantiated, the properties are correctly set, the event
        mask is modified, and the instance variables are initialized.

        @param cb: An instance of the main Application class.
        @type cb: pida.main.Application.
        """
        gtk.Window.__init__(self)
        # Window needs to be realized to do anything useful with it. Realizing
        # does not show the window to the user, so we can use it, but not have
        # an ugly blank frame while it loads.
        self.realize()
        # The "Vim" property
        self.window.property_change("Vim", gdk.SELECTION_TYPE_STRING, 8,
                            gdk.PROP_MODE_REPLACE, "6.0")
        # Set the correct event mask and connect the notify event
        self.add_events(gtk.gdk.PROPERTY_CHANGE_MASK)
        self.connect('property-notify-event', self.cb_notify)
        # The serial number used for sending synchronous messages
        self.serial = 1
        # A dictionary of callbacks for synchronous messages. The key is the
        # serial number, and the value is a callable that will be called with
        # the result of the synchronous evaluation.
        self.callbacks = {}
        # A dictionary to store the working directories for each Vim so they
        # only have to be fetched once.
        self.server_cwds = {}
        # A list of the last fetched server list, used todecide whther to feed
        # the most recent server list to the client.
        self.oldservers = None
        # An instance of the root window, so it only has to be fetched once.
        self.root_window = gdk.get_default_root_window()
        # Instantiate and start the hidden communication window, used for
        # fetching accurate and reliable server lists. 
        self.vim_hidden = VimHidden()
        self.vim_hidden.start()
        # Start the timer for fetching the server list at the specified number
        # of milliseconds (500).
        gobject.timeout_add(1000, self.fetch_serverlist)
    
    def fetch_serverlist(self):
        """
        Fetch the serverlist, and if it has changed, feed it to the client.

        The serverlist is requested asynchrnously, and passed the gotservers
        function as a callback. The gotservers function is then called with the
        server list, gets the appropriate working directory (if required) and
        feeds the new server list to the client if it has changed.
        """
        def gotservers(serverlist):
            """
            Called back on receiving the serverlist.

            Fetch working directories for new Vim instances, and feed the
            server list to the client if it has changed.
            """
            for server in serverlist:
                # Check if we already have the working directory.
                if server not in self.server_cwds:
                    # We don't, fetch it
                    self.fetch_cwd(server)
            # Check if the server list has changed
            if serverlist != self.oldservers:
                self.oldservers = serverlist
                # A ew serverlist to feed to the client.
                self.feed_serverlist(serverlist)
        # Fetch the server list from the hidden vim instance with the
        # gotservers function as a callback.
        self.get_hidden_serverlist(gotservers)
        # Return True so that the timer continues
        return True

    def get_rootwindow_serverlist(self):
        """
        Get the X root window's version of the current Vim serverlist.

        On starting with the client-server feature, GVim or Vim with the
        --servername option registers its server name and X window id as part
        of the "VimRegistry" parameter on the X root window.

        This method extracts and parses that property, and returns the server
        list.

        Note: Vim does not actually unregister itself with the root window on
        dying, so the presence of a server in the root window list is no
        gurantee that it is alive.

        @return: servers
        @rtype servers: dict of ("server", "window id") key, value
        """
        servers = {}
        # Read the property
        vimregistry = self.root_window.property_get("VimRegistry")
        # If it exists
        if vimregistry:
            # Get the list of servers by splitting with '\0'
            vimservers = vimregistry[-1].split('\0')
            # Parse each individual server and add to the results list
            for rawserver in vimservers:
                # Sometimes blank servers exist in the list
                if rawserver:
                    # split the raw value for the name and id
                    name_id = rawserver.split()
                    # Set the value in the results dict, remembering to convert
                    # the window id to a long int.
                    servers[name_id[1]] = long(int(name_id[0], 16))
        # return the list of resuts
        return servers

    def get_shell_serverlist(self):
        """
        Get the server list by starting console Vim on a Pipe.

        This blocks, so we don't use it. It is one of the alternative methods
        of retrieving an accurate serverlist. It is slow, and expensive.
        """
        vimcom = prop_main_registry.commands.vim_console.value()
        p = os.popen('%s --serverlist' % vimcom)
        servers = p.read()
        p.close()
        return servers.splitlines()
 
    def get_hidden_serverlist(self, callbackfunc):
        """
        Get the serverlist from the hidden Vim instance and call the callback
        function with the results.

        This method checks first whther the Vim instance is alive, and then
        evaluates the serverlist() function remotely in it, with a local call
        back function which parses the result and calls the user-provided
        callback function.

        @param callbackfunc: The call back function to be called with the
            server list.
        @type callbackfunc: callable
        """
        def cb(serverstring):
            """
            Called back with the raw server list.

            Parse the lines and call the call back function, ignoring any
            instances starting with "__" which represent hidden instances. If
            the hidden Vim instance is not alive, it is restarted.
            """
            servers = serverstring.splitlines()
            # Call the callback function
            callbackfunc([svr for svr in servers if not svr.startswith('__')])
        # Check if the hidden Vim is alive. 
        if self.vim_hidden.is_alive():
            # It is alive, get the serverlist.
            self.send_expr(self.vim_hidden.name, 'serverlist()', cb)
        else:
            # It is not alive, restart it.
            self.vim_hidden.start()
        
    def get_server_wid(self, servername):
        """
        Get the X Window id for a named Vim server.

        This function returns the id from the root window server list, if it
        exists, or None if it does not.

        @param servername: The name of the server
        @type servername: str

        @return: wid
        @rtype wid: long
        """
        try:
            # get the window id from the root window
            wid = self.get_rootwindow_serverlist()[servername]
        except KeyError:
            # The server is not registered in the root window so return None
            wid = None
        # Return wid if it is not none, or None
        return wid and long(wid) or None

    def get_server_window(self, wid):
        """
        Create and return a GDK window for a given window ID.
        
        This method simply calls gdk.window_foreign_new, which should return
        None if the window has been destroyed, but does not, in some cases.

        @param wid: The window ID.
        @type wid: long
        """
        return gtk.gdk.window_foreign_new(wid)

    def feed_serverlist(self, serverlist):
        """
        Feed the given list of servers to the client.

        This is achieved by calling the clients serverlist event. In Pida, this
        event is passed on to all the plugins.

        @param serverlist: The list of servers.
        @type serverlist: list
        """
        # Call the event.
        self.do_evt('serverlist', serverlist)

    def fetch_cwd(self, servername):
        """
        Fetch the working directory for a named server and store the result.
        """
        def gotcwd(cwd):
            """
            Called back on receiving the working directory, store it for later
            use.
            """
            self.server_cwds[servername] = cwd
        # Evaluate the expression with the gotcwd callback
        self.send_expr(servername, "getcwd()", gotcwd)

    def abspath(self, servername, filename):
        """
        Return the absolute path of a buffer name in the context of the named
        server.
        """
        # Only alter non-absolute paths
        if not filename.startswith('/'):
            try:
                # Try to find the current working directory
                cwd = self.server_cwds[servername]
            except KeyError:
                # The working directory is not set
                # Use a sane default, and fetch it
                cwd = os.path.expanduser('~')
                self.fetch_cwd(servername)
            filename = os.path.join(cwd, filename)
        return filename

    def generate_message(self, server, cork, message, sourceid):
        """
        Generate a message.
        """
        # Increment the serial number used for synchronous messages
        if cork:
            self.serial = self.serial + 1
            # Pick an arbitrary number where we recycle.
            if self.serial > 65530:
                self.serial = 1
        # return the generated string
        return '\0%s\0-n %s\0-s %s\0-r %x %s\0' % (cork,
                                                   server,
                                                   message,
                                                   sourceid,
                                                   self.serial)
 
    def parse_message(self, message):
        """
        Parse a received message and return the message atributes as a
        dictionary.
        """
        messageattrs = {}
        for t in [s.split(' ') for s in message.split('\0')]:
            if t and len(t[0]):
                if t[0].startswith('-'):
                    #attributes start with a '-', strip it and set the value
                    if len(t) > 1:
                        messageattrs[t[0][1:]] = t[1]
                else:
                    # Otherwise set the t attribute
                    messageattrs['t'] = t[0]
        return messageattrs


    def send_message(self, servername, message, asexpr, callback):
        wid = self.get_server_wid(servername)
        if wid:
            cork = (asexpr and 'c') or 'k'
            sw = self.get_server_window(wid)
            if sw and sw.property_get("Vim"):
                mp = self.generate_message(servername, cork, message,
                                        self.window.xid)
                sw.property_change("Comm", gdk.TARGET_STRING, 8,
                                        gdk.PROP_MODE_APPEND, mp)
                if asexpr and callback:
                    self.callbacks['%s' % (self.serial)] = callback

    def send_expr(self, server, message, callback):
        self.send_message(server, message, True, callback)

    def send_keys(self, server, message):
        self.send_message(server, message, False, False)

    def send_esc(self, server):
        self.send_keys(server, '<C-\><C-N>')

    def send_ret(self, server):
        self.send_keys(server, '<RETURN>')

    def send_ex(self, server, message):
        self.send_esc(server)
        self.send_keys(server, ':%s' % message)
        self.send_ret(server)

    def get_option(self, server, option, callbackfunc):
        self.send_expr(server, '&%s' % option, callbackfunc)
    

    def foreground(self, server):
        def cb(*args):
            pass
        self.send_expr(server, 'foreground()', cb)
        
    def change_buffer(self, server, nr):
        self.send_ex(server, 'b!%s' % nr)

    def close_buffer(self, server):
        self.send_ex(server, 'confirm bw')

    def change_cursor(self, server, x, y):
        self.send_message(server, 'cursor(%s, %s)' % (y, x), True, False)
        self.send_esc(server)

    def save_session(self, name):
        self.send_ex('mks %s' % name)

    def escape_filename(self, name):
        for s in ['\\', '?', '*', ' ', "'", '"', '[', '	', '$']:
            name = name.replace (s, '\\%s' % s)
        return name

    def open_file(self, server, name):
        self.send_ex(server, 'confirm e %s' % self.escape_filename(name))

    def preview_file(self, server, fn):
        self.send_ex(server, 'pc')
        self.send_ex(server, 'set nopreviewwindow')
        self.send_ex(server, 'pedit %s' % fn)

    def get_bufferlist(self, server):
        def cb(bl):
            if bl:
                l = [i.split(':') for i in bl.strip(';').split(';')]
                L = []
                for n in l:
                    if not n[0].startswith('E'):
                        L.append([n[0], self.abspath(server, n[1])])
                self.do_evt('bufferlist', L)
        #self.get_cwd(server)
        self.send_expr(server, 'Bufferlist()', cb)

    def get_current_buffer(self, server):
        def cb(bs):
            bn = bs.split(',')
            bn[1] = self.abspath(server, bn[1])
            self.do_evt('bufferchange', *bn)
        #self.get_cwd(server)
        self.send_expr(server, "bufnr('%').','.bufname('%')", cb)

    def quit(self, server):
        self.send_ex(server, 'q')

    def cb_notify(self, *a):
        win, ev =  a
        if hasattr(ev, 'atom'):
            if ev.atom == 'Comm':
                message = self.window.property_get('Comm', pdelete=True)
                if message:
                    self.cb_reply(message[-1])
        return True

    def cb_reply(self, data):
        mdict = self.parse_message(data)
        if mdict['t'] == 'r':
            if mdict['s'] in self.callbacks:
                self.callbacks[mdict['s']](mdict['r'])
        else:
            s = [t for t in data.split('\0') if t.startswith('-n')].pop()[3:]
            self.cb_reply_async(s)

    def cb_reply_async(self, data):
        if data.count(','):
            evt, d = data.split(',', 1)
            self.do_evt(evt, *d.split(','))
        else:
            self.do_log('bad async reply', data, 10)

