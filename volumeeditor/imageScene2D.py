#!/usr/bin/env python
# -*- coding: utf-8 -*-

#    Copyright 2010, 2011 C Sommer, C Straehle, U Koethe, FA Hamprecht. All rights reserved.
#    
#    Redistribution and use in source and binary forms, with or without modification, are
#    permitted provided that the following conditions are met:
#    
#       1. Redistributions of source code must retain the above copyright notice, this list of
#          conditions and the following disclaimer.
#    
#       2. Redistributions in binary form must reproduce the above copyright notice, this list
#          of conditions and the following disclaimer in the documentation and/or other materials
#          provided with the distribution.
#    
#    THIS SOFTWARE IS PROVIDED BY THE ABOVE COPYRIGHT HOLDERS ``AS IS'' AND ANY EXPRESS OR IMPLIED
#    WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND
#    FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE ABOVE COPYRIGHT HOLDERS OR
#    CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#    CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#    SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
#    ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
#    NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
#    ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#    
#    The views and conclusions contained in the software and documentation are those of the
#    authors and should not be interpreted as representing official policies, either expressed
#    or implied, of their employers.

from functools import partial
from PyQt4.QtCore import QRect, QRectF, QMutex, QPointF, Qt
from PyQt4.QtGui import QGraphicsScene, QImage, QTransform, QPen, QColor, QBrush

from patchAccessor import PatchAccessor
from imageSceneRendering import ImageSceneRenderThread

#*******************************************************************************
# I m a g e P a t c h                                                          *
#*******************************************************************************

class ImagePatch(object):    
    """
    A patch that makes up the whole 2D scene as displayed in ImageScene2D.
   
    An ImagePatch has a bounding box (self.rect, self.rectF) and
    its image content is either represented by a QImage
    
    When the current image content becomes invalid or is currently
    being overwritten, the patch becomes dirty.
    """ 
    
    def __init__(self, rectF, id):
        assert(type(rectF) == QRectF)
        
        self.rectF  = rectF
        self.rect   = QRect(round(rectF.x()),     round(rectF.y()), \
                            round(rectF.width()), round(rectF.height()))
        self.image  = QImage(self.rect.width(), self.rect.height(), QImage.Format_ARGB32_Premultiplied)
        self.image.fill(0)
        self.dirty = True
        self.id = id
        self._mutex = QMutex()
    def lock(self):
        self._mutex.lock()
    def unlock(self):
        self._mutex.unlock()

#*******************************************************************************
# I m a g e S c e n e 2 D                                                      *
#*******************************************************************************

class ImageScene2D(QGraphicsScene):
    """
    The 2D scene description of a tiled image generated by evaluating
    an overlay stack, together with a 2D cursor.
    """
    
    # base patch size: blockSize x blockSize
    blockSize = 128
    # overlap between patches 
    # positive number prevents rendering artifacts between patches for certain zoom levels
    # increases the base blockSize
    #
    # caution: an overlap will pull in multiple surrounding patches 
    overlap = 0
    
    @property
    def stackedImageSources(self):
        return self._stackedImageSources
    
    @stackedImageSources.setter
    def stackedImageSources(self, s):
        self._stackedImageSources = s
        s.isDirty.connect(self._invalidateRect)
        self._initializePatches()
        s.stackChanged.connect(partial(self._invalidateRect, QRect()))
        s.aboutToResize.connect(self._onAboutToResize)
        self._numLayers = len(s)
        self._initializePatches()

    def _onAboutToResize(self, newSize):
        print "<_onAboutToResize(newSize=%d), %r>" % (newSize, self)
        self._renderThread.stop()
        self._numLayers = newSize
        self._initializePatches()
        self._renderThread.start()
        print "</_onAboutToResize, %r>" % self

    @property
    def showDebugPatches(self):
        return self._showDebugPatches
    @showDebugPatches.setter
    def showDebugPatches(self, show):
        self._showDebugPatches = show
        self._invalidateRect()

    @property
    def sceneShape(self):
        """
        The shape of the scene in QGraphicsView's coordinate system.
        """
        return (self.sceneRect().width(), self.sceneRect().height())
    @sceneShape.setter
    def sceneShape(self, sceneShape):
        """
        Set the size of the scene in QGraphicsView's coordinate system.
        sceneShape -- (widthX, widthY),
        where the origin of the coordinate system is in the upper left corner
        of the screen and 'x' points right and 'y' points down
        """   
            
        assert len(sceneShape) == 2
        self.setSceneRect(0,0, *sceneShape)
        self.addRect(QRectF(0,0,*sceneShape), pen=QPen(QColor(255,0,0)))
        
        #The scene shape is in Qt's QGraphicsScene coordinate system,
        #that is the origin is in the top left of the screen, and the
        #'x' axis points to the right and the 'y' axis down.
        
        #The coordinate system of the data handles things differently.
        #The x axis points down and the y axis points to the right.
        
        r = self.scene2data.mapRect(QRect(0,0,sceneShape[0], sceneShape[1]))
        sliceShape = (r.width(), r.height())
        
        del self._renderThread
        del self._imagePatches
        
        self._patchAccessor = PatchAccessor(sliceShape[0], sliceShape[1], blockSize=self.blockSize)
            
        self._renderThread = ImageSceneRenderThread(self.stackedImageSources, parent=self)
        self._renderThread.start()
        
        self._renderThread.patchAvailable.connect(self._schedulePatchRedraw)
        
        self._initializePatches()

    def setBrush(self, b):
        self._brush = b

    def __init__( self ):
        QGraphicsScene.__init__(self)
        self._updatableTiles = []

        # tiled rendering of patches
        self._imagePatches = None
        self._renderThread = None
        self._stackedImageSources = None
        self._numLayers = 0 #current number of 'layers'
        self._showDebugPatches = False
    
        self.data2scene = QTransform(0,1,1,0,0,0) 
        self.scene2data = self.data2scene.transposed()
    
        def cleanup():
            self._renderThread.stop()
        self.destroyed.connect(cleanup)
    
    def _initializePatches(self):
        if not self._renderThread:
            return
              
        self._renderThread.stop()
        
        self._imagePatches = []
        #add an additional layer for the final composited image patch
        for layerNr in range(self._numLayers+2):
            self._imagePatches.append(list())
            for patchNr in range(self._patchAccessor.patchCount):
                rect = self._patchAccessor.patchRectF(patchNr, self.overlap)
                sceneRect = self.data2scene.mapRect(rect)
                #the patch accessor uses the data coordinate system
                #
                #because the patch is drawn on the screen, its holds coordinates
                #corresponding to Qt's QGraphicsScene's system
                #convert to scene coordinates
                self._imagePatches[layerNr].append( ImagePatch(sceneRect, patchNr ))
        
        self._renderThread._imagePatches = self._imagePatches
        
        self._renderThread.start()
    
    def compositePatches(self):
        return self._imagePatches[self._numLayers]
    def brushingPatches(self):
        return self._imagePatches[self._numLayers+1]
            
    def _invalidateRect(self, rect = QRect()):
        if not rect.isValid():
            #everything is invalidated
            #we cancel all requests
            self._renderThread.cancelAll()
            self._updatableTiles = []
            
            for p in self.brushingPatches():
                p.lock()
                p.image.fill(0)
                p.dirty = False
                p.unlock()
        
        for p in self.compositePatches():
            if not rect.isValid() or rect.intersects(p.rect):
                #convention: if a rect is invalid, it is infinitely large
                p.dirty = True
                self._schedulePatchRedraw(p.id)

    def _schedulePatchRedraw(self, patchNr):
        p =  self.compositePatches()[patchNr]
        
        #in QGraphicsScene::update, which is triggered by the
        #invalidate call below, the code
        #
        #view->d_func()->updateRectF(view->viewportTransform().mapRect(rect))
        #
        #seems to introduce rounding errors to the mapped rectangle.
        #
        #While we invalidate only one patch's rect, the rounding errors
        #enlarge the rect slightly, so that when update() is triggered
        #the neighbouring patches are also affected.
        #
        #To compensate, adjust the rectangle slightly (less than one pixel,
        #so it should not matter) 
        
        self.invalidate(p.rectF.adjusted(0.3,0.3,-0.3,-0.3), QGraphicsScene.BackgroundLayer)

    def drawForeground(self, painter, rect):
        for p in self.brushingPatches():
            if not p.dirty or not p.rectF.intersect(rect): continue
            p.lock()
            painter.drawImage(p.rectF.topLeft(), p.image)
            p.unlock()
    
    def drawBackground(self, painter, rect):
        #Find all patches that intersect the given 'rect'.
        for p in self.compositePatches():
            if p.dirty and rect.intersects(p.rectF):
                if self._showDebugPatches:
                    print "ImageScene2D '%s' asks for patch=%d [%r]" % (self.objectName(), p.id, p.rect)
                self._renderThread.requestPatch(p.id)
        
        for p in self.compositePatches():
            
            if not p.rectF.intersect(rect):
                continue
            
            p.lock()
            painter.drawImage(p.rectF.topLeft(), p.image)
            p.unlock()

            if self._showDebugPatches:
                if p.dirty:
                    painter.setBrush(QBrush(QColor(255,0,0), Qt.DiagCrossPattern))
                    painter.setPen(QColor(255,255,255))
                else:
                    painter.setBrush(QBrush(QColor(0,255,0), Qt.NoBrush))
                    painter.setPen(QColor(0,255,0))
                adjRect = p.rectF.adjusted(5,5,-5,-5)
                painter.drawRect(adjRect)
                painter.drawText(p.rectF.topLeft()+QPointF(20,20), "%d" % p.id)
                    