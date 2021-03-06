# Copyright 2009-2010 Gregory P. Ward
# Copyright 2009-2010 Intelerad Medical Systems Incorporated
# Copyright 2010-2011 Fog Creek Software
# Copyright 2010-2011 Unity Technologies
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

'''setup for largefiles repositories: reposetup'''
import copy
import os

from mercurial import error, manifest, match as match_, util
from mercurial.i18n import _
from mercurial import localrepo, scmutil

import lfcommands
import lfutil

def reposetup(ui, repo):
    # wire repositories should be given new wireproto functions
    # by "proto.wirereposetup()" via "hg.wirepeersetupfuncs"
    if not repo.local():
        return

    class lfilesrepo(repo.__class__):
        lfstatus = False
        def status_nolfiles(self, *args, **kwargs):
            return super(lfilesrepo, self).status(*args, **kwargs)

        # When lfstatus is set, return a context that gives the names
        # of largefiles instead of their corresponding standins and
        # identifies the largefiles as always binary, regardless of
        # their actual contents.
        def __getitem__(self, changeid):
            ctx = super(lfilesrepo, self).__getitem__(changeid)
            if self.lfstatus:
                class lfilesmanifestdict(manifest.manifestdict):
                    def __contains__(self, filename):
                        orig = super(lfilesmanifestdict, self).__contains__
                        return orig(filename) or orig(lfutil.standin(filename))
                class lfilesctx(ctx.__class__):
                    def files(self):
                        filenames = super(lfilesctx, self).files()
                        return [lfutil.splitstandin(f) or f for f in filenames]
                    def manifest(self):
                        man1 = super(lfilesctx, self).manifest()
                        man1.__class__ = lfilesmanifestdict
                        return man1
                    def filectx(self, path, fileid=None, filelog=None):
                        orig = super(lfilesctx, self).filectx
                        try:
                            if filelog is not None:
                                result = orig(path, fileid, filelog)
                            else:
                                result = orig(path, fileid)
                        except error.LookupError:
                            # Adding a null character will cause Mercurial to
                            # identify this as a binary file.
                            if filelog is not None:
                                result = orig(lfutil.standin(path), fileid,
                                              filelog)
                            else:
                                result = orig(lfutil.standin(path), fileid)
                            olddata = result.data
                            result.data = lambda: olddata() + '\0'
                        return result
                ctx.__class__ = lfilesctx
            return ctx

        # Figure out the status of big files and insert them into the
        # appropriate list in the result. Also removes standin files
        # from the listing. Revert to the original status if
        # self.lfstatus is False.
        # XXX large file status is buggy when used on repo proxy.
        # XXX this needs to be investigated.
        @localrepo.unfilteredmethod
        def status(self, node1='.', node2=None, match=None, ignored=False,
                clean=False, unknown=False, listsubrepos=False):
            listignored, listclean, listunknown = ignored, clean, unknown
            orig = super(lfilesrepo, self).status
            if not self.lfstatus:
                return orig(node1, node2, match, listignored, listclean,
                            listunknown, listsubrepos)

            # some calls in this function rely on the old version of status
            self.lfstatus = False
            ctx1 = self[node1]
            ctx2 = self[node2]
            working = ctx2.rev() is None
            parentworking = working and ctx1 == self['.']

            if match is None:
                match = match_.always(self.root, self.getcwd())

            wlock = None
            try:
                try:
                    # updating the dirstate is optional
                    # so we don't wait on the lock
                    wlock = self.wlock(False)
                except error.LockError:
                    pass

                # First check if there were files specified on the
                # command line.  If there were, and none of them were
                # largefiles, we should just bail here and let super
                # handle it -- thus gaining a big performance boost.
                lfdirstate = lfutil.openlfdirstate(ui, self)
                if match.files() and not match.anypats():
                    for f in lfdirstate:
                        if match(f):
                            break
                    else:
                        return orig(node1, node2, match, listignored, listclean,
                                    listunknown, listsubrepos)

                # Create a copy of match that matches standins instead
                # of largefiles.
                def tostandins(files):
                    if not working:
                        return files
                    newfiles = []
                    dirstate = self.dirstate
                    for f in files:
                        sf = lfutil.standin(f)
                        if sf in dirstate:
                            newfiles.append(sf)
                        elif sf in dirstate.dirs():
                            # Directory entries could be regular or
                            # standin, check both
                            newfiles.extend((f, sf))
                        else:
                            newfiles.append(f)
                    return newfiles

                m = copy.copy(match)
                m._files = tostandins(m._files)

                result = orig(node1, node2, m, ignored, clean, unknown,
                              listsubrepos)
                if working:

                    def sfindirstate(f):
                        sf = lfutil.standin(f)
                        dirstate = self.dirstate
                        return sf in dirstate or sf in dirstate.dirs()

                    match._files = [f for f in match._files
                                    if sfindirstate(f)]
                    # Don't waste time getting the ignored and unknown
                    # files from lfdirstate
                    unsure, s = lfdirstate.status(match, [], False, listclean,
                                                  False)
                    (modified, added, removed, clean) = (s.modified, s.added,
                                                         s.removed, s.clean)
                    if parentworking:
                        for lfile in unsure:
                            standin = lfutil.standin(lfile)
                            if standin not in ctx1:
                                # from second parent
                                modified.append(lfile)
                            elif ctx1[standin].data().strip() \
                                    != lfutil.hashfile(self.wjoin(lfile)):
                                modified.append(lfile)
                            else:
                                if listclean:
                                    clean.append(lfile)
                                lfdirstate.normal(lfile)
                    else:
                        tocheck = unsure + modified + added + clean
                        modified, added, clean = [], [], []
                        checkexec = self.dirstate._checkexec

                        for lfile in tocheck:
                            standin = lfutil.standin(lfile)
                            if standin in ctx1:
                                abslfile = self.wjoin(lfile)
                                if ((ctx1[standin].data().strip() !=
                                     lfutil.hashfile(abslfile)) or
                                    (checkexec and
                                     ('x' in ctx1.flags(standin)) !=
                                     bool(lfutil.getexecutable(abslfile)))):
                                    modified.append(lfile)
                                elif listclean:
                                    clean.append(lfile)
                            else:
                                added.append(lfile)

                        # at this point, 'removed' contains largefiles
                        # marked as 'R' in the working context.
                        # then, largefiles not managed also in the target
                        # context should be excluded from 'removed'.
                        removed = [lfile for lfile in removed
                                   if lfutil.standin(lfile) in ctx1]

                    # Standins no longer found in lfdirstate has been
                    # removed
                    for standin in ctx1.walk(lfutil.getstandinmatcher(self)):
                        lfile = lfutil.splitstandin(standin)
                        if not match(lfile):
                            continue
                        if lfile not in lfdirstate:
                            removed.append(lfile)

                    # Filter result lists
                    result = list(result)

                    # Largefiles are not really removed when they're
                    # still in the normal dirstate. Likewise, normal
                    # files are not really removed if they are still in
                    # lfdirstate. This happens in merges where files
                    # change type.
                    removed = [f for f in removed
                               if f not in self.dirstate]
                    result[2] = [f for f in result[2]
                                 if f not in lfdirstate]

                    lfiles = set(lfdirstate._map)
                    # Unknown files
                    result[4] = set(result[4]).difference(lfiles)
                    # Ignored files
                    result[5] = set(result[5]).difference(lfiles)
                    # combine normal files and largefiles
                    normals = [[fn for fn in filelist
                                if not lfutil.isstandin(fn)]
                               for filelist in result]
                    lfstatus = (modified, added, removed, s.deleted, [], [],
                                clean)
                    result = [sorted(list1 + list2)
                              for (list1, list2) in zip(normals, lfstatus)]
                else: # not against working directory
                    result = [[lfutil.splitstandin(f) or f for f in items]
                              for items in result]

                if wlock:
                    lfdirstate.write()

            finally:
                if wlock:
                    wlock.release()

            self.lfstatus = True
            return scmutil.status(*result)

        # As part of committing, copy all of the largefiles into the
        # cache.
        def commitctx(self, *args, **kwargs):
            node = super(lfilesrepo, self).commitctx(*args, **kwargs)
            lfutil.copyalltostore(self, node)
            return node

        # Before commit, largefile standins have not had their
        # contents updated to reflect the hash of their largefile.
        # Do that here.
        def commit(self, text="", user=None, date=None, match=None,
                force=False, editor=False, extra={}):
            orig = super(lfilesrepo, self).commit

            wlock = self.wlock()
            try:
                # Case 0: Automated committing
                #
                # While automated committing (like rebase, transplant
                # and so on), this code path is used to avoid:
                # (1) updating standins, because standins should
                #     be already updated at this point
                # (2) aborting when stadnins are matched by "match",
                #     because automated committing may specify them directly
                #
                if getattr(self, "_isrebasing", False) or \
                        getattr(self, "_istransplanting", False):
                    result = orig(text=text, user=user, date=date, match=match,
                                    force=force, editor=editor, extra=extra)

                    if result:
                        lfdirstate = lfutil.openlfdirstate(ui, self)
                        for f in self[result].files():
                            if lfutil.isstandin(f):
                                lfile = lfutil.splitstandin(f)
                                lfutil.synclfdirstate(self, lfdirstate, lfile,
                                                      False)
                        lfdirstate.write()

                    return result
                # Case 1: user calls commit with no specific files or
                # include/exclude patterns: refresh and commit all files that
                # are "dirty".
                if ((match is None) or
                    (not match.anypats() and not match.files())):
                    # Spend a bit of time here to get a list of files we know
                    # are modified so we can compare only against those.
                    # It can cost a lot of time (several seconds)
                    # otherwise to update all standins if the largefiles are
                    # large.
                    lfdirstate = lfutil.openlfdirstate(ui, self)
                    dirtymatch = match_.always(self.root, self.getcwd())
                    unsure, s = lfdirstate.status(dirtymatch, [], False, False,
                                                  False)
                    modifiedfiles = unsure + s.modified + s.added + s.removed
                    lfiles = lfutil.listlfiles(self)
                    # this only loops through largefiles that exist (not
                    # removed/renamed)
                    for lfile in lfiles:
                        if lfile in modifiedfiles:
                            if os.path.exists(
                                    self.wjoin(lfutil.standin(lfile))):
                                # this handles the case where a rebase is being
                                # performed and the working copy is not updated
                                # yet.
                                if os.path.exists(self.wjoin(lfile)):
                                    lfutil.updatestandin(self,
                                        lfutil.standin(lfile))
                                    lfdirstate.normal(lfile)

                    result = orig(text=text, user=user, date=date, match=match,
                                    force=force, editor=editor, extra=extra)

                    if result is not None:
                        for lfile in lfdirstate:
                            if lfile in modifiedfiles:
                                if (not os.path.exists(self.wjoin(
                                   lfutil.standin(lfile)))) or \
                                   (not os.path.exists(self.wjoin(lfile))):
                                    lfdirstate.drop(lfile)

                    # This needs to be after commit; otherwise precommit hooks
                    # get the wrong status
                    lfdirstate.write()
                    return result

                lfiles = lfutil.listlfiles(self)
                match._files = self._subdirlfs(match.files(), lfiles)

                # Case 2: user calls commit with specified patterns: refresh
                # any matching big files.
                smatcher = lfutil.composestandinmatcher(self, match)
                standins = self.dirstate.walk(smatcher, [], False, False)

                # No matching big files: get out of the way and pass control to
                # the usual commit() method.
                if not standins:
                    return orig(text=text, user=user, date=date, match=match,
                                    force=force, editor=editor, extra=extra)

                # Refresh all matching big files.  It's possible that the
                # commit will end up failing, in which case the big files will
                # stay refreshed.  No harm done: the user modified them and
                # asked to commit them, so sooner or later we're going to
                # refresh the standins.  Might as well leave them refreshed.
                lfdirstate = lfutil.openlfdirstate(ui, self)
                for standin in standins:
                    lfile = lfutil.splitstandin(standin)
                    if lfdirstate[lfile] != 'r':
                        lfutil.updatestandin(self, standin)
                        lfdirstate.normal(lfile)
                    else:
                        lfdirstate.drop(lfile)

                # Cook up a new matcher that only matches regular files or
                # standins corresponding to the big files requested by the
                # user.  Have to modify _files to prevent commit() from
                # complaining "not tracked" for big files.
                match = copy.copy(match)
                origmatchfn = match.matchfn

                # Check both the list of largefiles and the list of
                # standins because if a largefile was removed, it
                # won't be in the list of largefiles at this point
                match._files += sorted(standins)

                actualfiles = []
                for f in match._files:
                    fstandin = lfutil.standin(f)

                    # ignore known largefiles and standins
                    if f in lfiles or fstandin in standins:
                        continue

                    actualfiles.append(f)
                match._files = actualfiles

                def matchfn(f):
                    if origmatchfn(f):
                        return f not in lfiles
                    else:
                        return f in standins

                match.matchfn = matchfn
                result = orig(text=text, user=user, date=date, match=match,
                                force=force, editor=editor, extra=extra)
                # This needs to be after commit; otherwise precommit hooks
                # get the wrong status
                lfdirstate.write()
                return result
            finally:
                wlock.release()

        def push(self, remote, force=False, revs=None, newbranch=False):
            if remote.local():
                missing = set(self.requirements) - remote.local().supported
                if missing:
                    msg = _("required features are not"
                            " supported in the destination:"
                            " %s") % (', '.join(sorted(missing)))
                    raise util.Abort(msg)
            return super(lfilesrepo, self).push(remote, force=force, revs=revs,
                newbranch=newbranch)

        def _subdirlfs(self, files, lfiles):
            '''
            Adjust matched file list
            If we pass a directory to commit whose only commitable files
            are largefiles, the core commit code aborts before finding
            the largefiles.
            So we do the following:
            For directories that only have largefiles as matches,
            we explicitly add the largefiles to the match list and remove
            the directory.
            In other cases, we leave the match list unmodified.
            '''
            actualfiles = []
            dirs = []
            regulars = []

            for f in files:
                if lfutil.isstandin(f + '/'):
                    raise util.Abort(
                        _('file "%s" is a largefile standin') % f,
                        hint=('commit the largefile itself instead'))
                # Scan directories
                if os.path.isdir(self.wjoin(f)):
                    dirs.append(f)
                else:
                    regulars.append(f)

            for f in dirs:
                matcheddir = False
                d = self.dirstate.normalize(f) + '/'
                # Check for matched normal files
                for mf in regulars:
                    if self.dirstate.normalize(mf).startswith(d):
                        actualfiles.append(f)
                        matcheddir = True
                        break
                if not matcheddir:
                    # If no normal match, manually append
                    # any matching largefiles
                    for lf in lfiles:
                        if self.dirstate.normalize(lf).startswith(d):
                            actualfiles.append(lf)
                            if not matcheddir:
                                actualfiles.append(lfutil.standin(f))
                                matcheddir = True
                # Nothing in dir, so readd it
                # and let commit reject it
                if not matcheddir:
                    actualfiles.append(f)

            # Always add normal files
            actualfiles += regulars
            return actualfiles

    repo.__class__ = lfilesrepo

    def prepushoutgoinghook(local, remote, outgoing):
        if outgoing.missing:
            toupload = set()
            addfunc = lambda fn, lfhash: toupload.add(lfhash)
            lfutil.getlfilestoupload(local, outgoing.missing, addfunc)
            lfcommands.uploadlfiles(ui, local, remote, toupload)
    repo.prepushoutgoinghooks.add("largefiles", prepushoutgoinghook)

    def checkrequireslfiles(ui, repo, **kwargs):
        if 'largefiles' not in repo.requirements and util.any(
                lfutil.shortname+'/' in f[0] for f in repo.store.datafiles()):
            repo.requirements.add('largefiles')
            repo._writerequirements()

    ui.setconfig('hooks', 'changegroup.lfiles', checkrequireslfiles,
                 'largefiles')
    ui.setconfig('hooks', 'commit.lfiles', checkrequireslfiles, 'largefiles')
