// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/progress-steps/ProgressSteps.tsx MIT
//
// The process steps scene (task 4.3): two to five steps in order, each a
// numbered accent node with its label and an optional detail, and an arrow
// from each step to the next. Only the choreography comes from the source
// above, no code: the steps light up one after another, each arrow drawing
// on toward the next node as its step lands, all within the kit's entrance.
// Without an image the steps stand side by side in TEXT_BOX, each in a
// column under its node, with the arrows running between the nodes; beside
// an image they stack in the left column, one per row, the arrows pointing
// down the node column.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Arrow, Enter, Lines, Mark, Picture, Pop, SceneText, column, fit} from './layout.tsx';
import type {SceneProps, Theme} from './layout.tsx';

const KIND = 'process-steps';
// The widest step column without an image; five steps share TEXT_BOX.
const COLUMN = 480;

type Step = {label: string; detail?: string};
type Content = {steps: Step[]};

// Step i lands half a beat after step i − 1; its arrow and text follow.
const beatOf = (i: number) => i / 2;

const Node = ({n, size, theme}: {n: number; size: number; theme: Theme}) => (
	<Pop
		kind={KIND}
		beat={beatOf(n - 1)}
		theme={theme}
		style={{
			width: size,
			height: size,
			borderRadius: size,
			display: 'flex',
			alignItems: 'center',
			justifyContent: 'center',
			backgroundColor: theme.brand.accent,
		}}
	>
		<Mark text={String(n)} size="caption" weight="bold" color={theme.brand.bg} theme={theme} />
	</Pop>
);

// Side by side: a column per step, its node on one row with the others and
// its text centred beneath; each arrow runs from its node to the next one.
const Across = ({steps, theme}: {steps: Step[]; theme: Theme}) => {
	const {brand, spacing} = theme;
	const width = Math.min(COLUMN, Math.floor(column(null) / steps.length));
	const node = spacing[5];
	const gap = spacing[1];
	return (
		<div style={{display: 'flex', alignItems: 'flex-start'}}>
			{steps.map((step, i) => (
				<div key={i} style={{position: 'relative', width, display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
					<Node n={i + 1} size={node} theme={theme} />
					{i === steps.length - 1 ? null : (
						<div
							style={{
								position: 'absolute',
								left: (width + node) / 2 + gap,
								top: 0,
								width: width - node - 2 * gap,
								height: node,
								display: 'flex',
								alignItems: 'center',
							}}
						>
							<Arrow kind={KIND} beat={beatOf(i) + 1 / 4} theme={theme} direction="right" color={brand.accent} style={{flex: 1}} />
						</div>
					)}
					<Enter
						kind={KIND}
						beat={beatOf(i) + 1 / 4}
						theme={theme}
						style={{display: 'flex', flexDirection: 'column', alignItems: 'center', gap: spacing[0], marginTop: spacing[2]}}
					>
						<Lines
							text={step.label}
							maxWidth={fit(width, 'body', theme)}
							size="body"
							weight="bold"
							color={brand.text}
							align="center"
							theme={theme}
						/>
						{step.detail === undefined ? null : (
							<Lines
								text={step.detail}
								maxWidth={fit(width, 'caption', theme)}
								size="caption"
								color={brand.dim}
								align="center"
								theme={theme}
							/>
						)}
					</Enter>
				</div>
			))}
		</div>
	);
};

// Stacked: a row per step, its node beside its label and its detail
// beneath; the arrow to the next step runs down the node column beside the
// detail.
const Down = ({steps, width, theme}: {steps: Step[]; width: number; theme: Theme}) => {
	const {brand, spacing} = theme;
	const node = spacing[4];
	const gap = spacing[2];
	const text = width - node - gap;
	return (
		<div style={{display: 'flex', flexDirection: 'column'}}>
			{steps.map((step, i) => {
				const last = i === steps.length - 1;
				return (
					<div key={i} style={{display: 'flex', flexDirection: 'column'}}>
						<div style={{display: 'flex', alignItems: 'center', gap}}>
							<Node n={i + 1} size={node} theme={theme} />
							<Enter kind={KIND} beat={beatOf(i) + 1 / 4} theme={theme}>
								<Lines
									text={step.label}
									maxWidth={fit(text, 'body', theme)}
									size="body"
									weight="bold"
									color={brand.text}
									theme={theme}
								/>
							</Enter>
						</div>
						{last && step.detail === undefined ? null : (
							<div style={{display: 'flex', alignItems: 'stretch', gap, minHeight: last ? 0 : spacing[5]}}>
								<div
									style={{
										width: node,
										flexShrink: 0,
										display: 'flex',
										flexDirection: 'column',
										alignItems: 'center',
										padding: `${spacing[0]}px 0`,
									}}
								>
									{last ? null : (
										<Arrow
											kind={KIND}
											beat={beatOf(i) + 1 / 4}
											theme={theme}
											direction="down"
											color={brand.accent}
											style={{flex: 1}}
										/>
									)}
								</div>
								{step.detail === undefined ? null : (
									<Enter kind={KIND} beat={beatOf(i) + 1 / 4} theme={theme} style={{paddingBottom: last ? 0 : gap}}>
										<Lines
											text={step.detail}
											maxWidth={fit(text, 'caption', theme)}
											size="caption"
											color={brand.dim}
											theme={theme}
										/>
									</Enter>
								)}
							</div>
						)}
					</div>
				);
			})}
		</div>
	);
};

export const ProcessSteps = ({content, image, theme}: SceneProps<Content>) => {
	const width = column(image);
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align={image === null ? 'center' : 'left'}>
				{image === null ? <Across steps={content.steps} theme={theme} /> : <Down steps={content.steps} width={width} theme={theme} />}
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
