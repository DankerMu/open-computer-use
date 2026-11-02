// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Applies to `npx remotion` commands run from this directory.

import {Config} from '@remotion/cli/config';

// Projects symlink node_modules to the root-owned /opt/remotion-template, so
// the default webpack cache in node_modules/.cache is not writable.
Config.setCachingEnabled(false);
