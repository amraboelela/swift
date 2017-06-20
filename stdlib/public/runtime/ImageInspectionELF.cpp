//===--- ImageInspectionELF.cpp - ELF image inspection --------------------===//
//
// This source file is part of the Swift.org open source project
//
// Copyright (c) 2014 - 2017 Apple Inc. and the Swift project authors
// Licensed under Apache License v2.0 with Runtime Library Exception
//
// See https://swift.org/LICENSE.txt for license information
// See https://swift.org/CONTRIBUTORS.txt for the list of Swift project authors
//
//===----------------------------------------------------------------------===//
///
/// \file
///
/// This file includes routines that interact with ld*.so on ELF-based platforms
/// to extract runtime metadata embedded in dynamically linked ELF images
/// generated by the Swift compiler.
///
//===----------------------------------------------------------------------===//

#if defined(__ELF__) || defined(__ANDROID__)

#include "ImageInspection.h"
#include "swift/Runtime/Debug.h"
#include <dlfcn.h>
#include <elf.h>
#include <link.h>
#include <string.h>

using namespace swift;

/// The symbol name in the image that identifies the beginning of the
/// protocol conformances table.
static const char ProtocolConformancesSymbol[] =
  ".swift2_protocol_conformances_start";
/// The symbol name in the image that identifies the beginning of the
/// type metadata record table.
static const char TypeMetadataRecordsSymbol[] =
  ".swift2_type_metadata_start";

/// Context arguments passed down from dl_iterate_phdr to its callback.
struct InspectArgs {
  /// Symbol name to look up.
  const char *symbolName;
  /// Callback function to invoke with the metadata block.
  void (*addBlock)(const void *start, uintptr_t size);
  /// Set to true when initialize*Lookup() is called.
  bool didInitializeLookup;
};

static InspectArgs ProtocolConformanceArgs = {
  ProtocolConformancesSymbol,
  addImageProtocolConformanceBlockCallback,
  false
};

static InspectArgs TypeMetadataRecordArgs = {
  TypeMetadataRecordsSymbol,
  addImageTypeMetadataRecordBlockCallback,
  false
};


// Extract the section information for a named section in an image. imageName
// can be nullptr to specify the main executable.
static SectionInfo getSectionInfo(const char *imageName,
                                  const char *sectionName) {
  SectionInfo sectionInfo = { 0, nullptr };
  void *handle = dlopen(imageName, RTLD_LAZY | RTLD_NOLOAD);
  if (!handle) {
#ifdef __ANDROID__
    return sectionInfo;
#else
    fatalError(/* flags = */ 0, "dlopen() failed on `%s': %s", imageName,
               dlerror());
#endif
  }
  void *symbol = dlsym(handle, sectionName);
  if (symbol) {
    // Extract the size of the section data from the head of the section.
    const char *section = reinterpret_cast<const char *>(symbol);
    memcpy(&sectionInfo.size, section, sizeof(uint64_t));
    sectionInfo.data = section + sizeof(uint64_t);
  }
  dlclose(handle);
  return sectionInfo;
}

static int iteratePHDRCallback(struct dl_phdr_info *info,
                               size_t size, void *data) {
  InspectArgs *inspectArgs = reinterpret_cast<InspectArgs *>(data);
  const char *fname = info->dlpi_name;

  // While dl_iterate_phdr() is in progress it holds a lock to prevent other
  // images being loaded. The initialize flag is set here inside the callback so
  // that addNewDSOImage() sees a consistent state. If it was set outside the
  // dl_iterate_phdr() call then it could result in images being missed or
  // added twice.
  inspectArgs->didInitializeLookup = true;

  if (fname == nullptr || fname[0] == '\0') {
    // The filename may be null for both the dynamic loader and main executable.
    // So ignore null image name here and explicitly add the main executable
    // in initialize*Lookup() to avoid adding the data twice.
    return 0;
  }

  SectionInfo block = getSectionInfo(fname, inspectArgs->symbolName);
  if (block.size > 0) {
    inspectArgs->addBlock(block.data, block.size);
  }
  return 0;
}

// Add the section information in an image specified by an address in that
// image.
static void addBlockInImage(const InspectArgs *inspectArgs, const void *addr) {
  const char *fname = nullptr;
  if (addr) {
    Dl_info info;
    if (dladdr(addr, &info) == 0 || info.dli_fname == nullptr) {
      return;
    }
    fname = info.dli_fname;
  }
  SectionInfo block = getSectionInfo(fname, inspectArgs->symbolName);
  if (block.size > 0) {
    inspectArgs->addBlock(block.data, block.size);
  }
}

static void initializeSectionLookup(InspectArgs *inspectArgs) {
  // Add section data in the main executable.
  addBlockInImage(inspectArgs, nullptr);
  // Search the loaded dls. This only searches the already
  // loaded ones. Any images loaded after this are processed by
  // addNewDSOImage() below.
  dl_iterate_phdr(iteratePHDRCallback, reinterpret_cast<void *>(inspectArgs));
}

void swift::initializeProtocolConformanceLookup() {
  initializeSectionLookup(&ProtocolConformanceArgs);
}

void swift::initializeTypeMetadataRecordLookup() {
  initializeSectionLookup(&TypeMetadataRecordArgs);
}

// As ELF images are loaded, ImageInspectionInit:sectionDataInit() will call
// addNewDSOImage() with an address in the image that can later be used via
// dladdr() to dlopen() the image after the appropriate initialize*Lookup()
// function has been called.
SWIFT_RUNTIME_EXPORT
void swift_addNewDSOImage(const void *addr) {
  if (ProtocolConformanceArgs.didInitializeLookup) {
    addBlockInImage(&ProtocolConformanceArgs, addr);
  }

  if (TypeMetadataRecordArgs.didInitializeLookup) {
    addBlockInImage(&TypeMetadataRecordArgs, addr);
  }
}

int swift::lookupSymbol(const void *address, SymbolInfo *info) {
  Dl_info dlinfo;
  if (dladdr(address, &dlinfo) == 0) {
    return 0;
  }

  info->fileName = dlinfo.dli_fname;
  info->baseAddress = dlinfo.dli_fbase;
  info->symbolName = dlinfo.dli_sname;
  info->symbolAddress = dlinfo.dli_saddr;
  return 1;
}

#endif // defined(__ELF__) || defined(__ANDROID__)
