CXX ?= c++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -pedantic
CPPFLAGS ?= -Iinclude

BUILD_DIR := build
FEATURE_TOOL := $(BUILD_DIR)/extract_features

.PHONY: all clean

all: $(FEATURE_TOOL)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(FEATURE_TOOL): tools/extract_features.cpp src/rf_features.cpp include/rf_features.hpp | $(BUILD_DIR)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) tools/extract_features.cpp src/rf_features.cpp -o $@

clean:
	rm -rf $(BUILD_DIR)
