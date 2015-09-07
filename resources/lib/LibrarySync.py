#################################################################################################
# LibrarySync
#################################################################################################

import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs
import json
import sqlite3
import inspect
import threading
import urllib
from datetime import datetime, timedelta, time
from distutils.version import LooseVersion, StrictVersion
from itertools import chain
import urllib2
import os

import KodiMonitor
from API import API
import Utils as utils
from ClientInformation import ClientInformation
from DownloadUtils import DownloadUtils
from ReadEmbyDB import ReadEmbyDB
from ReadKodiDB import ReadKodiDB
from WriteKodiVideoDB import WriteKodiVideoDB
from WriteKodiMusicDB import WriteKodiMusicDB
from VideoNodes import VideoNodes

addondir = xbmc.translatePath(xbmcaddon.Addon(id='plugin.video.emby').getAddonInfo('profile'))
dataPath = os.path.join(addondir,"library")
movieLibrary = os.path.join(dataPath,'movies')
tvLibrary = os.path.join(dataPath,'tvshows')

WINDOW = xbmcgui.Window( 10000 )

class LibrarySync(threading.Thread):

    _shared_state = {}

    KodiMonitor = KodiMonitor.Kodi_Monitor()
    clientInfo = ClientInformation()

    addonName = clientInfo.getAddonName()

    updateItems = []
    userdataItems = []
    removeItems = []

    def __init__(self, *args):

        self.__dict__ = self._shared_state
        threading.Thread.__init__(self, *args)

    def logMsg(self, msg, lvl=1):

        className = self.__class__.__name__
        utils.logMsg("%s %s" % (self.addonName, className), msg, int(lvl))
        
    def FullLibrarySync(self,manualRun=False):
        
        startupDone = WINDOW.getProperty("startup") == "done"
        syncInstallRunDone = utils.settings("SyncInstallRunDone") == "true"
        performMusicSync = utils.settings("enableMusicSync") == "true"
        dbSyncIndication = utils.settings("dbSyncIndication") == "true"

        ### BUILD VIDEO NODES LISTING ###
        VideoNodes().buildVideoNodesListing()
        ### CREATE SOURCES ###
        if utils.settings("Sources") != "true":
            # Only create sources once
            self.logMsg("Sources.xml created.", 0)
            utils.createSources()
            utils.settings("Sources", "true")  
        
        # just do a incremental sync if that is what is required
        if(utils.settings("useIncSync") == "true" and utils.settings("SyncInstallRunDone") == "true") and manualRun == False:
            utils.logMsg("Sync Database", "Using incremental sync instead of full sync useIncSync=True)", 0)
            
            du = DownloadUtils()
            
            lastSync = utils.settings("LastIncrenetalSync")
            if(lastSync == None or len(lastSync) == 0):
                lastSync = "2010-01-01T00:00:00Z"
            utils.logMsg("Sync Database", "Incremental Sync Setting Last Run Time Loaded : " + lastSync, 0)

            lastSync = urllib2.quote(lastSync)
            
            url = "{server}/Emby.Kodi.SyncQueue/{UserId}/GetItems?LastUpdateDT=" + lastSync + "&format=json"
            utils.logMsg("Sync Database", "Incremental Sync Get Items URL : " + url, 0)
            
            try:
                results = du.downloadUrl(url)
                changedItems = results["ItemsUpdated"] + results["ItemsAdded"]
                removedItems = results["ItemsRemoved"]
                userChanges = results["UserDataChanged"]                
            except:
                utils.logMsg("Sync Database", "Incremental Sync Get Changes Failed", 0)
                pass
            else:
                maxItems = int(utils.settings("incSyncMaxItems"))
                utils.logMsg("Sync Database", "Incremental Sync Changes : " + str(results), 0)
                if(len(changedItems) < maxItems and len(removedItems) < maxItems and len(userChanges) < maxItems):
                
                    WINDOW.setProperty("startup", "done")
                    
                    LibrarySync().remove_items(removedItems)
                    LibrarySync().update_items(changedItems)
                    LibrarySync().user_data_update(userChanges)
                    
                    self.SaveLastSync()
                    
                    return True
                else:
                    utils.logMsg("Sync Database", "Too Many For Incremental Sync (" + str(maxItems) + "), changedItems" + str(len(changedItems)) + " removedItems:" + str(len(removedItems)) + " userChanges:" + str(len(userChanges)), 0)
        
        #set some variable to check if this is the first run
        WINDOW.setProperty("SyncDatabaseRunning", "true")     
        
        #show the progress dialog
        pDialog = None
        if (syncInstallRunDone == False or dbSyncIndication or manualRun):
            pDialog = xbmcgui.DialogProgressBG()
            pDialog.create('Emby for Kodi', 'Performing full sync')
        
        if(WINDOW.getProperty("SyncDatabaseShouldStop") ==  "true"):
            utils.logMsg("Sync Database", "Can not start SyncDatabaseShouldStop=True", 0)
            return True

        try:
            completed = True
                        
            ### PROCESS VIDEO LIBRARY ###
            
            #create the sql connection to video db
            connection = utils.KodiSQL("video")
            cursor = connection.cursor()
            
            #Add the special emby table
            cursor.execute("CREATE TABLE IF NOT EXISTS emby(emby_id TEXT, kodi_id INTEGER, media_type TEXT, checksum TEXT, parent_id INTEGER, kodi_file_id INTEGER)")
            try:
                cursor.execute("ALTER TABLE emby ADD COLUMN kodi_file_id INTEGER")
            except: pass
            connection.commit()
            
            # sync movies
            self.MoviesFullSync(connection,cursor,pDialog)
            
            if (self.ShouldStop()):
                return False
            
            #sync Tvshows and episodes
            self.TvShowsFullSync(connection,cursor,pDialog)
            
            if (self.ShouldStop()):
                return False
                    
            # sync musicvideos
            self.MusicVideosFullSync(connection,cursor,pDialog)
            
            #close sql connection
            cursor.close()
            
            ### PROCESS MUSIC LIBRARY ###
            if performMusicSync:
                #create the sql connection to music db
                connection = utils.KodiSQL("music")
                cursor = connection.cursor()
                
                #Add the special emby table
                cursor.execute("CREATE TABLE IF NOT EXISTS emby(emby_id TEXT, kodi_id INTEGER, media_type TEXT, checksum TEXT, parent_id INTEGER, kodi_file_id INTEGER)")
                try:
                    cursor.execute("ALTER TABLE emby ADD COLUMN kodi_file_id INTEGER")
                except: pass
                connection.commit()
                
                self.MusicFullSync(connection,cursor,pDialog)
                cursor.close()
            
            # set the install done setting
            if(syncInstallRunDone == False and completed):
                utils.settings("SyncInstallRunDone", "true")
                utils.settings("dbCreatedWithVersion", self.clientInfo.getVersion())    
            
            # Commit all DB changes at once and Force refresh the library
            xbmc.executebuiltin("UpdateLibrary(video)")
            #xbmc.executebuiltin("UpdateLibrary(music)")
            
            # set prop to show we have run for the first time
            WINDOW.setProperty("startup", "done")
            
            # tell any widgets to refresh because the content has changed
            WINDOW.setProperty("widgetreload", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            
            self.SaveLastSync()
            
        finally:
            WINDOW.setProperty("SyncDatabaseRunning", "false")
            utils.logMsg("Sync DB", "syncDatabase Exiting", 0)

        if(pDialog != None):
            pDialog.close()
        
        return True
        
    def SaveLastSync(self):
        # save last sync time

        du = DownloadUtils()    
        url = "{server}/Emby.Kodi.SyncQueue/GetServerDateTime?format=json"
            
        try:
            results = du.downloadUrl(url)
            lastSync = results["ServerDateTime"]
            self.logMsg("Sync Database, Incremental Sync Using Server Time: %s" % lastSync, 0)
            lastSync = datetime.strptime(lastSync, "%Y-%m-%dT%H:%M:%SZ")
            lastSync = (lastSync - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
            self.logMsg("Sync Database, Incremental Sync Using Server Time -5 min: %s" % lastSync, 0)
        except:
            lastSync = (datetime.utcnow() - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
            self.logMsg("Sync Database, Incremental Sync Using Client Time -5 min: %s" % lastSync, 0)
            
        self.logMsg("Sync Database, Incremental Sync Setting Last Run Time Saved: %s" % lastSync, 0)
        utils.settings("LastIncrenetalSync", lastSync)

    def MoviesFullSync(self,connection, cursor, pDialog):
               
        views = ReadEmbyDB().getCollections("movies")
        
        allKodiMovieIds = list()
        allEmbyMovieIds = list()
        
        for view in views:
            
            allEmbyMovies = ReadEmbyDB().getMovies(view.get('id'))
            allKodiMovies = ReadKodiDB().getKodiMovies(connection, cursor)
            
            for kodimovie in allKodiMovies:
                allKodiMovieIds.append(kodimovie[1])
            
            total = len(allEmbyMovies) + 1
            count = 1
            
            #### PROCESS ADDS AND UPDATES ###
            for item in allEmbyMovies:
                
                if (self.ShouldStop()):
                    return False
                
                if not item.get('IsFolder'):                    
                    allEmbyMovieIds.append(item["Id"])
                    
                    if(pDialog != None):
                        progressTitle = "Processing " + view.get('title') + " (" + str(count) + " of " + str(total) + ")"
                        percentage = int(((float(count) / float(total)) * 100))
                        pDialog.update(percentage, "Emby for Kodi - Running Sync", progressTitle)
                        count += 1        
                    
                    kodiMovie = None
                    for kodimovie in allKodiMovies:
                        if kodimovie[1] == item["Id"]:
                            kodiMovie = kodimovie
                          
                    if kodiMovie == None:
                        WriteKodiVideoDB().addOrUpdateMovieToKodiLibrary(item["Id"],connection, cursor, view.get('title'))
                    else:
                        if kodiMovie[2] != API().getChecksum(item):
                            WriteKodiVideoDB().addOrUpdateMovieToKodiLibrary(item["Id"],connection, cursor, view.get('title'))
          
          
       
        #### PROCESS BOX SETS #####
        utils.logMsg("Sync Movies", "BoxSet Sync Started", 1)
        boxsets = ReadEmbyDB().getBoxSets()
            
        total = len(boxsets) + 1
        count = 1
        for boxset in boxsets:
            if(pDialog != None):
                progressTitle = "Processing BoxSets" + " (" + str(count) + " of " + str(total-1) + ")"
                percentage = int(((float(count) / float(total)) * 100))
                pDialog.update(percentage, "Emby for Kodi - Running Sync", progressTitle)
                count += 1
            if(self.ShouldStop()):
                return False                
            boxsetMovies = ReadEmbyDB().getMoviesInBoxSet(boxset["Id"])
            WriteKodiVideoDB().addBoxsetToKodiLibrary(boxset, connection, cursor)
                
            WriteKodiVideoDB().removeMoviesFromBoxset(boxset, connection, cursor)
            for boxsetMovie in boxsetMovies:
                if(self.ShouldStop()):
                    return False
                WriteKodiVideoDB().updateBoxsetToKodiLibrary(boxsetMovie,boxset, connection, cursor)
                    
        utils.logMsg("Sync Movies", "BoxSet Sync Finished", 1)
            
        #### PROCESS DELETES #####
        allEmbyMovieIds = set(allEmbyMovieIds)
        for kodiId in allKodiMovieIds:
            if not kodiId in allEmbyMovieIds:
                WINDOW.setProperty(kodiId,"deleted")
                WriteKodiVideoDB().deleteItemFromKodiLibrary(kodiId, connection, cursor)
                
        ### commit all changes to database ###
        connection.commit()

    def MusicVideosFullSync(self,connection,cursor, pDialog):
               
        allKodiMusicvideoIds = list()
        allEmbyMusicvideoIds = list()
            
        allEmbyMusicvideos = ReadEmbyDB().getMusicVideos()
        allKodiMusicvideos = ReadKodiDB().getKodiMusicVideos(connection, cursor)
        
        for kodivideo in allKodiMusicvideos:
            allKodiMusicvideoIds.append(kodivideo[1])
        
        total = len(allEmbyMusicvideos) + 1
        count = 1
        
        #### PROCESS ADDS AND UPDATES ###
        for item in allEmbyMusicvideos:
            
            if (self.ShouldStop()):
                return False
            
            if not item.get('IsFolder'):                    
                allEmbyMusicvideoIds.append(item["Id"])
                
                if(pDialog != None):
                    progressTitle = "Processing MusicVideos (" + str(count) + " of " + str(total) + ")"
                    percentage = int(((float(count) / float(total)) * 100))
                    pDialog.update(percentage, "Emby for Kodi - Running Sync", progressTitle)
                    count += 1        
                
                kodiVideo = None
                for kodivideo in allKodiMusicvideos:
                    if kodivideo[1] == item["Id"]:
                        kodiVideo = kodivideo
                      
                if kodiVideo == None:
                    WriteKodiVideoDB().addOrUpdateMusicVideoToKodiLibrary(item["Id"],connection, cursor)
                else:
                    if kodiVideo[2] != API().getChecksum(item):
                        WriteKodiVideoDB().addOrUpdateMusicVideoToKodiLibrary(item["Id"],connection, cursor)
            
        #### PROCESS DELETES #####
        allEmbyMusicvideoIds = set(allEmbyMusicvideoIds)
        for kodiId in allKodiMusicvideoIds:
            if not kodiId in allEmbyMusicvideoIds:
                WINDOW.setProperty(kodiId,"deleted")
                WriteKodiVideoDB().deleteItemFromKodiLibrary(kodiId, connection, cursor)
                
        ### commit all changes to database ###
        connection.commit()
    
    def TvShowsFullSync(self,connection,cursor,pDialog):
               
        views = ReadEmbyDB().getCollections("tvshows")
        
        allKodiTvShowIds = list()
        allEmbyTvShowIds = list()
                
        for view in views:
            
            allEmbyTvShows = ReadEmbyDB().getTvShows(view.get('id'))
            allKodiTvShows = ReadKodiDB().getKodiTvShows(connection, cursor)
            
            total = len(allEmbyTvShows) + 1
            count = 1
            
            for kodishow in allKodiTvShows:
                allKodiTvShowIds.append(kodishow[1])
            
            #### TVSHOW: PROCESS ADDS AND UPDATES ###
            for item in allEmbyTvShows:
                
                if (self.ShouldStop()):
                    return False
                
                if(pDialog != None):
                    progressTitle = "Processing " + view.get('title') + " (" + str(count) + " of " + str(total) + ")"
                    percentage = int(((float(count) / float(total)) * 100))
                    pDialog.update(percentage, "Emby for Kodi - Running Sync", progressTitle)
                    count += 1                   

                if utils.settings('syncEmptyShows') == "true" or (item.get('IsFolder') and item.get('RecursiveItemCount') != 0):
                    allEmbyTvShowIds.append(item["Id"])
                    
                    #build a list with all Id's and get the existing entry (if exists) in Kodi DB
                    kodiShow = None
                    for kodishow in allKodiTvShows:
                        if kodishow[1] == item["Id"]:
                            kodiShow = kodishow
                          
                    if kodiShow == None:
                        # Tv show doesn't exist in Kodi yet so proceed and add it
                        WriteKodiVideoDB().addOrUpdateTvShowToKodiLibrary(item["Id"],connection, cursor, view.get('title'))
                    else:
                        # If there are changes to the item, perform a full sync of the item
                        if kodiShow[2] != API().getChecksum(item):
                            WriteKodiVideoDB().addOrUpdateTvShowToKodiLibrary(item["Id"],connection, cursor, view.get('title'))
                            
                    #### PROCESS EPISODES ######
                    self.EpisodesFullSync(connection,cursor,item["Id"])
            
        #### TVSHOW: PROCESS DELETES #####
        allEmbyTvShowIds = set(allEmbyTvShowIds)
        for kodiId in allKodiTvShowIds:
            if not kodiId in allEmbyTvShowIds:
                WINDOW.setProperty(kodiId,"deleted")
                WriteKodiVideoDB().deleteItemFromKodiLibrary(kodiId, connection, cursor)
                
        ### commit all changes to database ###
        connection.commit()
         
    def EpisodesFullSync(self,connection,cursor,showId):
        
        WINDOW = xbmcgui.Window( 10000 )
        
        allKodiEpisodeIds = list()
        allEmbyEpisodeIds = list()
        
        #get the kodi parent id
        cursor.execute("SELECT kodi_id FROM emby WHERE emby_id=?",(showId,))
        kodiShowId = cursor.fetchone()[0]
        
        allEmbyEpisodes = ReadEmbyDB().getEpisodes(showId)
        allKodiEpisodes = ReadKodiDB().getKodiEpisodes(connection, cursor, kodiShowId)
        
        for kodiepisode in allKodiEpisodes:
            allKodiEpisodeIds.append(kodiepisode[1])

        #### EPISODES: PROCESS ADDS AND UPDATES ###
        for item in allEmbyEpisodes:
            
            if (self.ShouldStop()):
                    return False    
            
            allEmbyEpisodeIds.append(item["Id"])
            
            #get the existing entry (if exists) in Kodi DB
            kodiEpisode = None
            for kodiepisode in allKodiEpisodes:
                if kodiepisode[1] == item["Id"]:
                    kodiEpisode = kodiepisode
                  
            if kodiEpisode == None:
                # Episode doesn't exist in Kodi yet so proceed and add it
                WriteKodiVideoDB().addOrUpdateEpisodeToKodiLibrary(item["Id"], kodiShowId, connection, cursor)
            else:
                # If there are changes to the item, perform a full sync of the item
                if kodiEpisode[2] != API().getChecksum(item):
                    WriteKodiVideoDB().addOrUpdateEpisodeToKodiLibrary(item["Id"], kodiShowId, connection, cursor)
        
        #### EPISODES: PROCESS DELETES #####
        allEmbyEpisodeIds = set(allEmbyEpisodeIds)
        for kodiId in allKodiEpisodeIds:
            if (not kodiId in allEmbyEpisodeIds):
                WINDOW.setProperty(kodiId,"deleted")
                WriteKodiVideoDB().deleteItemFromKodiLibrary(kodiId, connection, cursor)
                
    def MusicFullSync(self, connection,cursor, pDialog):

        self.ProcessMusicArtists(connection,cursor,pDialog)
        connection.commit()
        self.ProcessMusicAlbums(connection,cursor,pDialog)
        connection.commit()
        self.ProcessMusicSongs(connection,cursor,pDialog)
        
        ### commit all changes to database ###
        connection.commit()
    
    def ProcessMusicSongs(self,connection,cursor,pDialog):
               
        allKodiSongIds = list()
        allEmbySongIds = list()
        
        allEmbySongs = ReadEmbyDB().getMusicSongsTotal()
        allKodiSongs = ReadKodiDB().getKodiMusicSongs(connection, cursor)
        
        for kodisong in allKodiSongs:
            allKodiSongIds.append(kodisong[1])
            
        total = len(allEmbySongs) + 1
        count = 1    
        
        #### PROCESS SONGS ADDS AND UPDATES ###
        for item in allEmbySongs:
            
            if (self.ShouldStop()):
                return False
                             
            allEmbySongIds.append(item["Id"])
            
            if(pDialog != None):
                progressTitle = "Processing Music Songs (" + str(count) + " of " + str(total) + ")"
                percentage = int(((float(count) / float(total)) * 100))
                pDialog.update(percentage, "Emby for Kodi - Running Sync", progressTitle)
                count += 1        
            
            kodiSong = None
            for kodisong in allKodiSongs:
                if kodisong[1] == item["Id"]:
                    kodiSong = kodisong
                  
            if kodiSong == None:
                WriteKodiMusicDB().addOrUpdateSongToKodiLibrary(item,connection, cursor)
            else:
                if kodiSong[2] != API().getChecksum(item):
                    WriteKodiMusicDB().addOrUpdateSongToKodiLibrary(item,connection, cursor)
        
        #### PROCESS DELETES #####
        allEmbySongIds = set(allEmbySongIds)
        for kodiId in allKodiSongIds:
            if not kodiId in allEmbySongIds:
                WINDOW.setProperty(kodiId,"deleted")
                WriteKodiMusicDB().deleteItemFromKodiLibrary(kodiId, connection, cursor)
        
    def ProcessMusicArtists(self,connection,cursor,pDialog):
               
        allKodiArtistIds = list()
        allEmbyArtistIds = list()
        
        allEmbyArtists = ReadEmbyDB().getMusicArtistsTotal()
        allKodiArtists = ReadKodiDB().getKodiMusicArtists(connection, cursor)
        
        for kodiartist in allKodiArtists:
            allKodiArtistIds.append(kodiartist[1])
            
        total = len(allEmbyArtists) + 1
        count = 1    
        
        #### PROCESS ARTIST ADDS AND UPDATES ###
        for item in allEmbyArtists:
            
            if (self.ShouldStop()):
                return False
                             
            allEmbyArtistIds.append(item["Id"])
            
            if(pDialog != None):
                progressTitle = "Processing Music Artists (" + str(count) + " of " + str(total) + ")"
                percentage = int(((float(count) / float(total)) * 100))
                pDialog.update(percentage, "Emby for Kodi - Running Sync", progressTitle)
                count += 1        
            
            kodiArtist = None
            for kodiartist in allKodiArtists:
                if kodiartist[1] == item["Id"]:
                    kodiArtist = kodiartist
                  
            if kodiArtist == None:
                WriteKodiMusicDB().addOrUpdateArtistToKodiLibrary(item,connection, cursor)
            else:
                if kodiArtist[2] != API().getChecksum(item):
                    WriteKodiMusicDB().addOrUpdateArtistToKodiLibrary(item,connection, cursor)
        
        #### PROCESS DELETES #####
        allEmbyArtistIds = set(allEmbyArtistIds)
        for kodiId in allKodiArtistIds:
            if not kodiId in allEmbyArtistIds:
                WINDOW.setProperty(kodiId,"deleted")
                WriteKodiMusicDB().deleteItemFromKodiLibrary(kodiId, connection, cursor)
    
    def ProcessMusicAlbums(self,connection,cursor,pDialog):
               
        allKodiAlbumIds = list()
        allEmbyAlbumIds = list()
        
        allEmbyAlbums = ReadEmbyDB().getMusicAlbumsTotal()
        allKodiAlbums = ReadKodiDB().getKodiMusicAlbums(connection, cursor)
        
        for kodialbum in allKodiAlbums:
            allKodiAlbumIds.append(kodialbum[1])
            
        total = len(allEmbyAlbums) + 1
        count = 1    
        
        #### PROCESS SONGS ADDS AND UPDATES ###
        for item in allEmbyAlbums:
            
            if (self.ShouldStop()):
                return False
                             
            allEmbyAlbumIds.append(item["Id"])
            
            if(pDialog != None):
                progressTitle = "Processing Music Albums (" + str(count) + " of " + str(total) + ")"
                percentage = int(((float(count) / float(total)) * 100))
                pDialog.update(percentage, "Emby for Kodi - Running Sync", progressTitle)
                count += 1        
            
            kodiAlbum = None
            for kodialbum in allKodiAlbums:
                if kodialbum[1] == item["Id"]:
                    kodiAlbum = kodialbum
                  
            if kodiAlbum == None:
                WriteKodiMusicDB().addOrUpdateAlbumToKodiLibrary(item,connection, cursor)
            else:
                if kodiAlbum[2] != API().getChecksum(item):
                    WriteKodiMusicDB().addOrUpdateAlbumToKodiLibrary(item,connection, cursor)
        
        #### PROCESS DELETES #####
        allEmbyAlbumIds = set(allEmbyAlbumIds)
        for kodiId in allKodiAlbumIds:
            if not kodiId in allEmbyAlbumIds:
                WINDOW.setProperty(kodiId,"deleted")
                WriteKodiMusicDB().deleteItemFromKodiLibrary(kodiId, connection, cursor)
    
    def IncrementalSync(self, itemList):
        
        startupDone = WINDOW.getProperty("startup") == "done"
        
        #only perform incremental scan when full scan is completed 
        if startupDone:
        
            #this will only perform sync for items received by the websocket
            dbSyncIndication = utils.settings("dbSyncIndication") == "true"
            performMusicSync = utils.settings("enableMusicSync") == "true"
            WINDOW.setProperty("SyncDatabaseRunning", "true")
            
            #show the progress dialog               
            pDialog = None
            if (dbSyncIndication and xbmc.Player().isPlaying() == False):
                pDialog = xbmcgui.DialogProgressBG()
                pDialog.create('Emby for Kodi', 'Incremental Sync')
                self.logMsg("Doing LibraryChanged : Show Progress IncrementalSync()", 0);
            
            connection = utils.KodiSQL("video")
            cursor = connection.cursor()
            
            try:
                #### PROCESS MOVIES ####
                views = ReadEmbyDB().getCollections("movies")
                for view in views:
                    allEmbyMovies = ReadEmbyDB().getMovies(view.get('id'), itemList)
                    count = 1
                    total = len(allEmbyMovies) + 1
                    for item in allEmbyMovies:
                        if(pDialog != None):
                            progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                            percentage = int(((float(count) / float(total)) * 100))
                            pDialog.update(percentage, "Emby for Kodi - Incremental Sync Movies", progressTitle)
                            count = count + 1
                        if not item.get('IsFolder'):
                            WriteKodiVideoDB().addOrUpdateMovieToKodiLibrary(item["Id"],connection, cursor, view.get('title'))
                            
                #### PROCESS BOX SETS #####
                boxsets = ReadEmbyDB().getBoxSets()
                count = 1
                total = len(boxsets) + 1
                for boxset in boxsets:
                    if(boxset["Id"] in itemList):
                        utils.logMsg("IncrementalSync", "Updating box Set : " + str(boxset["Name"]), 1)
                        boxsetMovies = ReadEmbyDB().getMoviesInBoxSet(boxset["Id"])
                        WriteKodiVideoDB().addBoxsetToKodiLibrary(boxset, connection, cursor)
                        if(pDialog != None):
                            progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                            percentage = int(((float(count) / float(total)) * 100))
                            pDialog.update(percentage, "Emby for Kodi - Incremental Sync BoxSet", progressTitle)
                            count = count + 1
                        WriteKodiVideoDB().removeMoviesFromBoxset(boxset, connection, cursor)
                        for boxsetMovie in boxsetMovies:
                            WriteKodiVideoDB().updateBoxsetToKodiLibrary(boxsetMovie, boxset, connection, cursor)      
                    else:
                        utils.logMsg("IncrementalSync", "Skipping Box Set : " + boxset["Name"], 1)
                        
                #### PROCESS TV SHOWS ####
                views = ReadEmbyDB().getCollections("tvshows")              
                for view in views:
                    allEmbyTvShows = ReadEmbyDB().getTvShows(view.get('id'),itemList)
                    count = 1
                    total = len(allEmbyTvShows) + 1
                    for item in allEmbyTvShows:
                        if(pDialog != None):
                            progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                            percentage = int(((float(count) / float(total)) * 100))
                            pDialog.update(percentage, "Emby for Kodi - Incremental Sync Tv", progressTitle)
                            count = count + 1                    
                        if utils.settings('syncEmptyShows') == "true" or (item.get('IsFolder') and item.get('RecursiveItemCount') != 0):
                            kodiId = WriteKodiVideoDB().addOrUpdateTvShowToKodiLibrary(item["Id"],connection, cursor, view.get('title'))
                
                
                #### PROCESS OTHERS BY THE ITEMLIST ######
                count = 1
                total = len(itemList) + 1
                for item in itemList:
                        
                    if(pDialog != None):
                        progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                        percentage = int(((float(count) / float(total)) * 100))
                        pDialog.update(percentage, "Emby for Kodi - Incremental Sync Items", progressTitle)
                        count = count + 1                           
                        
                    MBitem = ReadEmbyDB().getItem(item)
                    itemType = MBitem.get('Type', "")

                    #### PROCESS EPISODES ######
                    if "Episode" in itemType:

                        #get the tv show
                        cursor.execute("SELECT kodi_id FROM emby WHERE media_type='tvshow' AND emby_id=?", (MBitem["SeriesId"],))
                        result = cursor.fetchone()
                        if result:
                            kodi_show_id = result[0]
                        else:
                            kodi_show_id = None

                        if kodi_show_id:
                            WriteKodiVideoDB().addOrUpdateEpisodeToKodiLibrary(MBitem["Id"], kodi_show_id, connection, cursor)
                        else:
                            #tv show doesn't exist
                            #perform full tvshow sync instead so both the show and episodes get added
                            self.TvShowsFullSync(connection,cursor,None)

                    elif "Season" in itemType:

                        #get the tv show
                        cursor.execute("SELECT kodi_id FROM emby WHERE media_type='tvshow' AND emby_id=?", (MBitem["SeriesId"],))
                        result = cursor.fetchone()
                        if result:
                            kodi_show_id = result[0]
                            # update season
                            WriteKodiVideoDB().updateSeasons(MBitem["SeriesId"], kodi_show_id, connection, cursor)
                    
                    #### PROCESS BOXSETS ######
                    elif "BoxSet" in itemType:
                        boxsetMovies = ReadEmbyDB().getMoviesInBoxSet(boxset["Id"])
                        WriteKodiVideoDB().addBoxsetToKodiLibrary(boxset,connection, cursor)
                        
                        for boxsetMovie in boxsetMovies:
                            WriteKodiVideoDB().updateBoxsetToKodiLibrary(boxsetMovie,boxset, connection, cursor)

                    #### PROCESS MUSICVIDEOS ####
                    elif "MusicVideo" in itemType:
                        if not MBitem.get('IsFolder'):                    
                            WriteKodiVideoDB().addOrUpdateMusicVideoToKodiLibrary(MBitem["Id"],connection, cursor)
                        
                ### commit all changes to database ###
                connection.commit()
                cursor.close()

                ### PROCESS MUSIC LIBRARY ###
                if performMusicSync:
                    connection = utils.KodiSQL("music")
                    cursor = connection.cursor()
                    for item in itemList:
                        MBitem = ReadEmbyDB().getItem(item)
                        itemType = MBitem.get('Type', "")
                        
                        if "MusicArtist" in itemType:
                            WriteKodiMusicDB().addOrUpdateArtistToKodiLibrary(MBitem, connection, cursor)
                        if "MusicAlbum" in itemType:
                            WriteKodiMusicDB().addOrUpdateAlbumToKodiLibrary(MBitem, connection, cursor)
                        if "Audio" in itemType:
                            WriteKodiMusicDB().addOrUpdateSongToKodiLibrary(MBitem, connection, cursor)    
                    connection.commit()
                    cursor.close()

            finally:
                if(pDialog != None):
                    pDialog.close()
                self.SaveLastSync()
                xbmc.executebuiltin("UpdateLibrary(video)")
                WINDOW.setProperty("SyncDatabaseRunning", "false")
                # tell any widgets to refresh because the content has changed
                WINDOW.setProperty("widgetreload", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def removefromDB(self, itemList, deleteEmbyItem = False):
    
        dbSyncIndication = utils.settings("dbSyncIndication") == "true"
    
        #show the progress dialog               
        pDialog = None
        if (dbSyncIndication and xbmc.Player().isPlaying() == False):
            pDialog = xbmcgui.DialogProgressBG()
            pDialog.create('Emby for Kodi', 'Incremental Sync')    
            self.logMsg("Doing LibraryChanged : Show Progress removefromDB()", 0);
            
        # Delete from Kodi before Emby
        # To be able to get mediaType
        doUtils = DownloadUtils()
        video = {}
        music = []
        
        # Database connection to myVideosXX.db
        connectionvideo = utils.KodiSQL()
        cursorvideo = connectionvideo.cursor()
        # Database connection to myMusicXX.db
        connectionmusic = utils.KodiSQL("music")
        cursormusic = connectionmusic.cursor()

        count = 1
        total = len(itemList) + 1          
        for item in itemList:
        
            if(pDialog != None):
                progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                percentage = int(((float(count) / float(total)) * 100))
                pDialog.update(percentage, "Emby for Kodi - Incremental Sync Delete ", progressTitle)
                count = count + 1   
                
            # Sort by type for database deletion
            try: # Search video database
                self.logMsg("Check video database.", 1)
                cursorvideo.execute("SELECT media_type FROM emby WHERE emby_id = ?", (item,))
                mediatype = cursorvideo.fetchone()[0]
                video[item] = mediatype
                #video.append(itemtype)
            except:
                self.logMsg("Check music database.", 1)
                try: # Search music database
                    cursormusic.execute("SELECT media_type FROM emby WHERE emby_id = ?", (item,))
                    cursormusic.fetchone()[0]
                    music.append(item)
                except: self.logMsg("Item %s is not found in Kodi database." % item, 1)

        if len(video) > 0:
            connection = connectionvideo
            cursor = cursorvideo
            # Process video library
            count = 1
            total = len(video) + 1                
            for item in video:

                if(pDialog != None):
                    progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                    percentage = int(((float(count) / float(total)) * 100))
                    pDialog.update(percentage, "Emby for Kodi - Incremental Sync Delete ", progressTitle)
                    count = count + 1   
                
                type = video[item]
                self.logMsg("Doing LibraryChanged: Items Removed: Calling deleteItemFromKodiLibrary: %s" % item, 1)

                if "episode" in type:
                    # Get the TV Show Id for reference later
                    showId = ReadKodiDB().getShowIdByEmbyId(item, connection, cursor)
                    self.logMsg("ShowId: %s" % showId, 1)
                WriteKodiVideoDB().deleteItemFromKodiLibrary(item, connection, cursor)
                # Verification
                if "episode" in type:
                    showTotalCount = ReadKodiDB().getShowTotalCount(showId, connection, cursor)
                    self.logMsg("ShowTotalCount: %s" % showTotalCount, 1)
                    # If there are no episodes left
                    if showTotalCount == 0 or showTotalCount == None:
                        # Delete show
                        embyId = ReadKodiDB().getEmbyIdByKodiId(showId, "tvshow", connection, cursor)
                        self.logMsg("Message: Doing LibraryChanged: Deleting show: %s" % embyId, 1)
                        WriteKodiVideoDB().deleteItemFromKodiLibrary(embyId, connection, cursor)

            connection.commit()
        # Close connection
        cursorvideo.close()

        if len(music) > 0:
            connection = connectionmusic
            cursor = cursormusic
            #Process music library
            if utils.settings('enableMusicSync') == "true":

                for item in music:
                    self.logMsg("Message : Doing LibraryChanged : Items Removed : Calling deleteItemFromKodiLibrary (musiclibrary): " + item, 0)
                    WriteKodiMusicDB().deleteItemFromKodiLibrary(item, connection, cursor)

                connection.commit()
        # Close connection
        cursormusic.close()

        if deleteEmbyItem:
            for item in itemList:
                url = "{server}/mediabrowser/Items/%s" % item
                self.logMsg('Deleting via URL: %s' % url)
                doUtils.downloadUrl(url, type = "DELETE")                            
                xbmc.executebuiltin("Container.Refresh")

        if(pDialog != None):
            pDialog.close()
        self.SaveLastSync()
        
        
    def setUserdata(self, listItems):
    
        dbSyncIndication = utils.settings("dbSyncIndication") == "true"
        musicenabled = utils.settings('enableMusicSync') == "true"
    
        #show the progress dialog               
        pDialog = None
        if (dbSyncIndication and xbmc.Player().isPlaying() == False):
            pDialog = xbmcgui.DialogProgressBG()
            pDialog.create('Emby for Kodi', 'Incremental Sync')
            self.logMsg("Doing LibraryChanged : Show Progress setUserdata()", 0);

        # We need to sort between video and music database
        video = []
        music = []
        # Database connection to myVideosXX.db
        connectionvideo = utils.KodiSQL()
        cursorvideo = connectionvideo.cursor()
        # Database connection to myMusicXX.db
        connectionmusic = utils.KodiSQL('music')
        cursormusic = connectionmusic.cursor()

        count = 1
        total = len(listItems) + 1        
        for userdata in listItems:
            # Sort between video and music
            itemId = userdata['ItemId']
                        
            if(pDialog != None):
                progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                percentage = int(((float(count) / float(total)) * 100))
                pDialog.update(percentage, "Emby for Kodi - Incremental Sync User Data ", progressTitle)
                count = count + 1               
            
            cursorvideo.execute("SELECT media_type FROM emby WHERE emby_id = ?", (itemId,))
            try: # Search video database
                self.logMsg("Check video database.", 2)
                mediatype = cursorvideo.fetchone()[0]
                video.append(userdata)
            except:
                if musicenabled:
                    cursormusic.execute("SELECT media_type FROM emby WHERE emby_id = ?", (itemId,))
                    try: # Search music database
                        self.logMsg("Check the music database.", 2)
                        mediatype = cursormusic.fetchone()[0]
                        music.append(userdata)
                    except: self.logMsg("Item %s is not found in Kodi database." % itemId, 1)
                else:
                    self.logMsg("Item %s is not found in Kodi database." % itemId, 1)

        if len(video) > 0:
            connection = connectionvideo
            cursor = cursorvideo
            # Process the userdata update for video library
            count = 1
            total = len(video) + 1              
            for userdata in video:
                if(pDialog != None):
                    progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                    percentage = int(((float(count) / float(total)) * 100))
                    pDialog.update(percentage, "Emby for Kodi - Incremental Sync User Data ", progressTitle)
                    count = count + 1
                WriteKodiVideoDB().updateUserdata(userdata, connection, cursor)

            connection.commit()
            xbmc.executebuiltin("UpdateLibrary(video)")
        # Close connection
        cursorvideo.close()

        if len(music) > 0:
            connection = connectionmusic
            cursor = cursormusic
            #Process music library
            count = 1
            total = len(video) + 1
            # Process the userdata update for music library
            if musicenabled:
                for userdata in music:
                    if(pDialog != None):
                        progressTitle = "Incremental Sync "+ " (" + str(count) + " of " + str(total) + ")"
                        percentage = int(((float(count) / float(total)) * 100))
                        pDialog.update(percentage, "Emby for Kodi - Incremental Sync User Data ", progressTitle)
                        count = count + 1
                    WriteKodiMusicDB().updateUserdata(userdata, connection, cursor)

                connection.commit()
                #xbmc.executebuiltin("UpdateLibrary(music)")
        # Close connection
        cursormusic.close()
        
        if(pDialog != None):
            pDialog.close()
        self.SaveLastSync()
                

    def remove_items(self, itemsRemoved):
        # websocket client
        if(len(itemsRemoved) > 0):
            self.logMsg("Doing LibraryChanged : Processing Deleted : " + str(itemsRemoved), 0)        
            self.removeItems.extend(itemsRemoved)

    def update_items(self, itemsToUpdate):
        # websocket client
        if(len(itemsToUpdate) > 0):
            self.logMsg("Doing LibraryChanged : Processing Added and Updated : " + str(itemsToUpdate), 0)
            self.updateItems.extend(itemsToUpdate)
            
    def user_data_update(self, userDataList):
        # websocket client
        if(len(userDataList) > 0):
            self.logMsg("Doing LibraryChanged : Processing User Data Changed : " + str(userDataList), 0)
            self.userdataItems.extend(userDataList)

    def ShouldStop(self):
            
        if(xbmc.abortRequested):
            return True

        if(WINDOW.getProperty("SyncDatabaseShouldStop") == "true"):
            return True

        return False

    def run(self):
        clientInfo = ClientInformation()
        self.logMsg("--- Starting Library Sync Thread ---", 0)
        WINDOW = xbmcgui.Window(10000)
        startupComplete = False

        while not self.KodiMonitor.abortRequested():

            # In the event the server goes offline after
            # the thread has already been started.
            while self.suspendClient == True:
                # The service.py will change self.suspendClient to False
                if self.KodiMonitor.waitForAbort(5):
                    # Abort was requested while waiting. We should exit
                    break

            # Check if the version of Emby for Kodi the DB was created with is recent enough - controled by Window property set at top of service _INIT_
            
            # START TEMPORARY CODE
            # Only get in here for a while, can be removed later
            if utils.settings("dbCreatedWithVersion")=="" and utils.settings("SyncInstallRunDone") == "true":
                return_value = xbmcgui.Dialog().yesno("DB Version", "Can't detect version of Emby for Kodi the DB was created with.\nWas it at least version " + WINDOW.getProperty('minDBVersion') + "?")
                if return_value == 0:
                    utils.settings("dbCreatedWithVersion","0.0.0")
                else:
                    utils.settings("dbCreatedWithVersion",WINDOW.getProperty('minDBVersion'))      
            # END TEMPORARY CODE
                
            if (utils.settings("SyncInstallRunDone") == "true" and LooseVersion(utils.settings("dbCreatedWithVersion")) < LooseVersion(WINDOW.getProperty('minDBVersion'))) and WINDOW.getProperty('minDBVersionCheck') != "true":
                return_value = xbmcgui.Dialog().yesno("DB Version", "Detected the DB needs to be recreated for\nthis version of Emby for Kodi.\nProceed?")
                if return_value == 0:
                    xbmcgui.Dialog().ok("Emby for Kodi","Emby for Kodi may not work\ncorrectly until the database is reset.\n")
                    WINDOW.setProperty('minDBVersionCheck', "true")
                else:
                    utils.reset()
            
            # Library sync
            if not startupComplete:
                # Run full sync
                self.logMsg("Doing_Db_Sync: syncDatabase (Started)", 1)
                startTime = datetime.now()
                libSync = self.FullLibrarySync()
                elapsedTime = datetime.now() - startTime
                self.logMsg("Doing_Db_Sync: syncDatabase (Finished in: %s) %s" % (str(elapsedTime).split('.')[0], libSync), 1)

                if libSync:
                    startupComplete = True

            # Set via Kodi Monitor event
            if WINDOW.getProperty("OnWakeSync") == "true" and WINDOW.getProperty('Server_online') == "true":
                WINDOW.clearProperty("OnWakeSync")
                if WINDOW.getProperty("SyncDatabaseRunning") != "true":
                    self.logMsg("Doing_Db_Sync Post Resume: syncDatabase (Started)", 0)
                    libSync = self.FullLibrarySync()
                    self.logMsg("Doing_Db_Sync Post Resume: syncDatabase (Finished) " + str(libSync), 0)

            

            if len(self.updateItems) > 0:
                # Add or update items
                self.logMsg("Processing items: %s" % (str(self.updateItems)), 1)
                listItems = self.updateItems
                self.updateItems = []
                self.IncrementalSync(listItems)

            if len(self.userdataItems) > 0:
                # Process userdata changes only
                self.logMsg("Processing items: %s" % (str(self.userdataItems)), 1)
                listItems = self.userdataItems
                self.userdataItems = []
                self.setUserdata(listItems)

            if len(self.removeItems) > 0:
                # Remove item from Kodi library
                self.logMsg("Removing items: %s" % self.removeItems, 1)
                listItems = self.removeItems
                self.removeItems = []
                self.removefromDB(listItems)

            if self.KodiMonitor.waitForAbort(1):
                # Abort was requested while waiting. We should exit
                break

        self.logMsg("--- Library Sync Thread stopped ---", 0)

    def suspendClient(self):
        self.suspendClient = True
        self.logMsg("--- Library Sync Thread paused ---", 0)

    def resumeClient(self):
        self.suspendClient = False
        self.logMsg("--- Library Sync Thread resumed ---", 0)